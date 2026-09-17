"""
LabelSegments - Queue-triggered Azure Function for AI-powered segment labeling

Triggered by a queue message (one per video) written by ManageLabels.
Each invocation processes one video's segments against every label in
label_defs, one label at a time: for each label, segments are chunked into
small batches and GPT judges just that one label against that one batch.
Results for all labels are accumulated in memory and written to the search
index once per video, after every label has been processed.
On completion of all videos, marks all labels as applied.

Queue message format:
  {
    "blob_name": "vid_xyz_segments.json",
    "label_defs": [{"name": ..., "description": ...}, ...],
    "valid_names": ["Label1", ...],
    "strip_names": ["Label1", "OldLabel", ...],
    "total": 47
  }

Environment Variables:
  AZURE_STORAGE_ACCOUNT   - Storage account name
  AZURE_STORAGE_KEY       - Storage account key
  LABELS_CONTAINER        - Blob container for label library (default: "labels")
  SEGMENTS_CONTAINER      - Blob container for segments (default: "segments")
  PROXY_BASE_URL          - Azure OpenAI proxy base URL
  FUNCTION_HOST_KEY       - Azure Function host key for the proxy
  SEARCH_ENDPOINT         - Azure AI Search endpoint
  SEARCH_ADMIN_KEY        - Azure AI Search admin key
  SEARCH_INDEX            - Search index name (default: "segments")
  BATCH_SIZE              - Segments per GPT call, per label (default: 10)
  GPT_WORKERS             - Parallel GPT calls per video (default: 5)
  LABEL_QUEUE_NAME        - Azure Storage Queue name (default: "label-jobs")
"""

import json
import logging
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Set

import azure.functions as func
import requests
from azure.storage.blob import BlobServiceClient
from openai import OpenAI

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", 10))
GPT_WORKERS = int(os.environ.get("GPT_WORKERS", 5))
INDEX_BATCH_SIZE = 500
SEARCH_API_VERSION = "2024-05-01-preview"


def _blob_service() -> BlobServiceClient:
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    key = os.environ["AZURE_STORAGE_KEY"]
    return BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=key,
    )


def _read_json_blob(container: str, blob_name: str) -> Any:
    service = _blob_service()
    bc = service.get_blob_client(container=container, blob=blob_name)
    return json.loads(bc.download_blob().readall())


def _fetch_existing_labels(segment_keys: List[str]) -> Dict[str, Dict]:
    """Fetch existing label assignments for this video's segment keys only."""
    if not segment_keys:
        return {}

    endpoint = os.environ["SEARCH_ENDPOINT"].rstrip("/")
    admin_key = os.environ["SEARCH_ADMIN_KEY"]
    index_name = os.environ.get("SEARCH_INDEX", "segments")
    url = f"{endpoint}/indexes/{index_name}/docs/search?api-version={SEARCH_API_VERSION}"
    headers = {"Content-Type": "application/json", "api-key": admin_key}

    existing = {}
    # Use search.in for efficient key lookup
    keys_str = "|".join(segment_keys)
    body = {
        "search": "*",
        "filter": f"search.in(segment_key, '{keys_str}', '|')",
        "select": "segment_key,pred_labels,pred_label_details",
        "top": len(segment_keys),
    }
    r = requests.post(url, headers=headers, json=body, timeout=60)
    if not r.ok:
        logging.warning(f"Failed to fetch existing labels: {r.status_code}")
        return existing

    for doc in r.json().get("value", []):
        seg_key = doc.get("segment_key", "")
        pred_labels = doc.get("pred_labels") or []
        raw_details = doc.get("pred_label_details") or "[]"
        try:
            pred_details = json.loads(raw_details)
            if not isinstance(pred_details, list):
                pred_details = []
        except Exception:
            pred_details = []
        existing[seg_key] = {"pred_labels": pred_labels, "pred_label_details": pred_details}

    return existing


def _call_gpt(label_def: Dict, seg_inputs: List[Dict]) -> List[Dict]:
    """
    Call GPT-4o-mini via proxy to judge one label against a batch of segments.
    Returns list of {segment_id, applied, rationale}.
    """
    client = OpenAI(
        base_url=os.environ["PROXY_BASE_URL"],
        api_key="unused",
        default_headers={"x-functions-key": os.environ["FUNCTION_HOST_KEY"]},
    )

    system_prompt = (
        "You are a content labeler. Given ONE label definition and a set of video transcript "
        "segments, decide whether that label applies to each segment. "
        "Return a decision for EVERY segment. "
        'Your response must be a JSON object with a "results" key containing an array.'
    )

    user_prompt = (
        f"Label:\n{json.dumps(label_def, ensure_ascii=False)}\n\n"
        f"Segments:\n{json.dumps(seg_inputs, ensure_ascii=False)}\n\n"
        "For each segment, return:\n"
        '  - "applied": true if the segment mentions, discusses, or is clearly related to the label topic\n'
        '  - "applied": false if the label does not apply\n'
        '  - "rationale": a brief explanation of your decision either way\n'
        "Note: where the label includes 'examples', use them as positive references for what the label looks like in practice.\n\n"
        "Return:\n"
        '{"results": [\n'
        '  {"segment_id": "0000", "applied": true, "rationale": "explanation"},\n'
        '  {"segment_id": "0001", "applied": false, "rationale": "explanation"}\n'
        "]}"
    )

    resp = client.responses.create(
        model="gpt-4o-mini",
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        text={"format": {"type": "json_object"}},
    )

    return json.loads(resp.output_text).get("results", [])


def _process_label_batch(
    label_def: Dict,
    batch: List[Dict],
    blob_name: str,
    batch_idx: int,
) -> Dict[str, Dict]:
    """Judge one label against one batch of segments, with retry.

    Returns {segment_id: {"applied": bool, "rationale": str}}, or {} on failure
    (existing labels for these segments are left untouched by the caller).
    """
    seg_inputs = [{"segment_id": s["segment_id"], "text": s["text"]} for s in batch]

    for attempt in range(2):
        try:
            raw_results = _call_gpt(label_def, seg_inputs)
            return {
                r["segment_id"]: {"applied": bool(r.get("applied")), "rationale": r.get("rationale", "")}
                for r in raw_results
                if isinstance(r, dict) and "segment_id" in r
            }
        except Exception as e:
            if attempt == 1:
                logging.warning(
                    f"GPT call failed for label '{label_def.get('name')}' on {blob_name} "
                    f"batch {batch_idx}: {e}"
                )
            else:
                time.sleep(2)

    return {}


def _build_docs(
    segments: List[Dict],
    video_id: str,
    strip_names: Set[str],
    existing_index: Dict[str, Dict],
    results_by_segment: Dict[str, Dict[str, Dict]],
) -> List[Dict[str, Any]]:
    """Merge this run's per-label decisions with existing labels into search index docs.

    results_by_segment is {segment_id: {label_name: {"applied": bool, "rationale": str}}},
    accumulated across ALL labels for the video before this runs once.
    """
    docs = []

    for s in segments:
        seg_id = s["segment_id"]
        segment_key = f"{video_id}_{seg_id}"

        if segment_key not in existing_index:
            logging.warning(f"Skipping {segment_key} — not found in search index, segment may not have been indexed")
            continue

        existing = existing_index.get(segment_key, {"pred_labels": [], "pred_label_details": []})
        kept_labels = [n for n in existing["pred_labels"] if n not in strip_names]
        kept_details = [d for d in existing["pred_label_details"] if isinstance(d, dict) and d.get("name") not in strip_names]

        seg_decisions = results_by_segment.get(seg_id, {})
        new_details = [
            {"name": name, "applied": dec["applied"], "rationale": dec["rationale"]}
            for name, dec in seg_decisions.items()
        ]
        new_applied = [name for name, dec in seg_decisions.items() if dec["applied"]]

        docs.append({
            "@search.action": "mergeOrUpload",
            "segment_key": segment_key,
            "pred_labels": kept_labels + new_applied,
            "pred_label_details": json.dumps(kept_details + new_details, ensure_ascii=False),
        })

    return docs


def _index_documents(docs: List[Dict[str, Any]]) -> None:
    endpoint = os.environ["SEARCH_ENDPOINT"].rstrip("/")
    admin_key = os.environ["SEARCH_ADMIN_KEY"]
    index_name = os.environ.get("SEARCH_INDEX", "segments")

    url = f"{endpoint}/indexes/{index_name}/docs/index?api-version={SEARCH_API_VERSION}"
    headers = {
        "Content-Type": "application/json",
        "api-key": admin_key,
    }

    r = requests.post(url, headers=headers, json={"value": docs}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Search indexing failed: {r.status_code} {r.text}")

    failed = [v for v in r.json().get("value", []) if not v.get("succeeded", True)]
    if failed:
        raise RuntimeError(f"Search indexing had failures: {failed[:3]}")


def _update_progress(labels_container: str) -> None:
    """Atomically increment completed counter. If last video, mark all labels applied."""
    service = _blob_service()
    bc = service.get_blob_client(container=labels_container, blob="labeling_status.json")

    # Retry acquiring the lease in case another invocation holds it
    lease = None
    for attempt in range(5):
        try:
            lease = bc.acquire_lease(lease_duration=15)
            break
        except Exception:
            if attempt == 4:
                raise
            time.sleep(1 + attempt)

    try:
        status = json.loads(bc.download_blob(lease=lease).readall())
        status["completed"] = status.get("completed", 0) + 1

        if status["completed"] >= status["total"]:
            status["status"] = "complete"
            label_bc = service.get_blob_client(container=labels_container, blob="label_library.json")
            library = json.loads(label_bc.download_blob().readall())
            for l in library.get("labels", []):
                if l.get("is_active", True):
                    l["applied"] = True
            library["removed_labels"] = []
            label_bc.upload_blob(json.dumps(library, ensure_ascii=False, indent=2), overwrite=True)

        bc.upload_blob(json.dumps(status, ensure_ascii=False), overwrite=True, lease=lease)
    finally:
        try:
            lease.release()
        except Exception:
            pass


def main(msg: func.QueueMessage) -> None:
    payload = json.loads(msg.get_body().decode("utf-8"))
    blob_name = payload["blob_name"]
    label_defs = payload["label_defs"]
    strip_names = set(payload["strip_names"])

    labels_container = os.environ.get("LABELS_CONTAINER", "labels")
    segments_container = os.environ.get("SEGMENTS_CONTAINER", "segments")

    try:
        data = _read_json_blob(segments_container, blob_name)
        if isinstance(data, dict):
            video_id = data.get("video_id")
            raw_segments = data.get("segments", [])
        elif isinstance(data, list):
            video_id = data[0].get("video_id") if data else None
            raw_segments = data
        else:
            logging.warning(f"Unexpected JSON schema: {blob_name}")
            return

        segments = [s for s in raw_segments if (s.get("text") or "").strip()]
        if not video_id or not segments:
            logging.warning(f"No valid segments in {blob_name}")
            return

        segment_keys = [f"{video_id}_{s['segment_id']}" for s in segments]
        existing_index = _fetch_existing_labels(segment_keys)

        # One task per (label, segment batch) — each GPT call judges a single
        # label against a small batch of segments.
        tasks = [
            (label_def, segments[i:i + BATCH_SIZE], i)
            for label_def in label_defs
            for i in range(0, len(segments), BATCH_SIZE)
        ]

        results_by_segment: Dict[str, Dict[str, Dict]] = defaultdict(dict)

        with ThreadPoolExecutor(max_workers=GPT_WORKERS) as executor:
            futures = {
                executor.submit(_process_label_batch, label_def, batch, blob_name, batch_idx): label_def["name"]
                for label_def, batch, batch_idx in tasks
            }
            for future in as_completed(futures):
                label_name = futures[future]
                try:
                    decisions = future.result()
                except Exception as e:
                    logging.warning(f"Label batch failed for '{label_name}' on {blob_name}: {e}")
                    continue
                for seg_id, decision in decisions.items():
                    results_by_segment[seg_id][label_name] = decision

        all_docs = _build_docs(segments, video_id, strip_names, existing_index, results_by_segment)

        for i in range(0, len(all_docs), INDEX_BATCH_SIZE):
            _index_documents(all_docs[i:i + INDEX_BATCH_SIZE])

        logging.info(f"Labeled {len(all_docs)} segments for {blob_name}")

    except Exception as e:
        logging.exception(f"Failed to process {blob_name}: {e}")
    finally:
        try:
            _update_progress(labels_container)
        except Exception as e:
            logging.warning(f"Failed to update progress for {blob_name}: {e}")
