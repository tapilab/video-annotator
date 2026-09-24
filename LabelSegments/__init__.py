"""
LabelSegments - Queue-triggered Azure Function for AI-powered segment labeling

Triggered by a queue message (one per video) written by ManageLabels.
The GPT call logic lives in shared/gpt_labeling.py
so EvalLabels can use exactly the same code.
Each invocation processes one video's segments against every label in
label_defs, one label at a time: for each label, segments are chunked into
small batches and GPT judges just that one label against that one batch.
Results for all labels are accumulated in memory and written to the search
index once per video, after every label has been processed.
When a video finishes it records a progress marker, including any labels whose
AI calls still failed after retries (those keep their old results on that video).
The last video to finish marks that run's labels as applied, flags labels with
failures as incomplete, and starts the next run if any are pending.

Queue message format:
  {
    "blob_name": "vid_xyz_segments.json",
    "round_id": "<uuid of the labeling run>",
    "label_defs": [{"name": ..., "description": ..., "examples": [...]}, ...],
    "strip_names": ["Label1", "OldLabel", ...]
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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Set

import azure.functions as func
import requests
from azure.storage.blob import BlobServiceClient

from shared.gpt_labeling import BATCH_SIZE, GPT_WORKERS, process_label_batch
from shared.label_overrides import apply_overrides, get_overrides_for_video
from shared.labeling_queue import mark_video_done, try_finish_round

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


def _read_label_library() -> Dict[str, Any]:
    service = _blob_service()
    container = os.environ.get("LABELS_CONTAINER", "labels")
    bc = service.get_blob_client(container=container, blob="label_library.json")
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


def _build_docs(
    segments: List[Dict],
    video_id: str,
    always_strip: Set[str],
    existing_index: Dict[str, Dict],
    results_by_segment: Dict[str, Dict[str, Dict]],
    overrides_by_segment: Dict[str, Dict[str, Dict]],
    id_to_name: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Merge this run's per-label decisions with existing labels into search index docs.

    results_by_segment is {segment_id: {label_name: {"applied": bool, "rationale": str}}},
    accumulated across ALL labels for the video before this runs once.

    pred_label_details always stays the AI's own honest record (untouched by
    manual overrides). pred_labels — the visible/filterable list — is derived
    fresh from it each time, with manual overrides layered on top, so a
    manual add/remove keeps applying on every future run, not just the one
    right after it was made.
    """
    docs = []

    for s in segments:
        seg_id = s["segment_id"]
        segment_key = f"{video_id}_{seg_id}"

        if segment_key not in existing_index:
            logging.warning(f"Skipping {segment_key} — not found in search index, segment may not have been indexed")
            continue

        existing = existing_index.get(segment_key, {"pred_labels": [], "pred_label_details": []})
        seg_decisions = results_by_segment.get(seg_id, {})
        strip = always_strip | set(seg_decisions)
        kept_details = [d for d in existing["pred_label_details"] if isinstance(d, dict) and d.get("name") not in strip]

        new_details = [
            {"name": name, "applied": dec["applied"], "rationale": dec["rationale"]}
            for name, dec in seg_decisions.items()
        ]
        final_details = kept_details + new_details

        seg_overrides = overrides_by_segment.get(seg_id, {})
        pred_labels = apply_overrides(final_details, seg_overrides, id_to_name)

        docs.append({
            "@search.action": "mergeOrUpload",
            "segment_key": segment_key,
            "pred_labels": pred_labels,
            "pred_label_details": json.dumps(final_details, ensure_ascii=False),
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


def main(msg: func.QueueMessage) -> None:
    payload = json.loads(msg.get_body().decode("utf-8"))
    blob_name = payload["blob_name"]
    round_id = payload["round_id"]
    label_defs = payload["label_defs"]
    strip_names = set(payload["strip_names"])

    labels_container = os.environ.get("LABELS_CONTAINER", "labels")
    segments_container = os.environ.get("SEGMENTS_CONTAINER", "segments")

    failed_labels: Set[str] = set()
    always_strip = strip_names - {d["name"] for d in label_defs}

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
        overrides_by_segment = get_overrides_for_video(video_id)
        label_library = _read_label_library()
        id_to_name = {
            l["label_id"]: l["name"] for l in label_library.get("labels", []) if l.get("is_active", True)
        }

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
                executor.submit(process_label_batch, label_def, batch, blob_name, batch_idx): label_def["name"]
                for label_def, batch, batch_idx in tasks
            }
            for future in as_completed(futures):
                label_name = futures[future]
                try:
                    decisions = future.result()
                except Exception as e:
                    logging.warning(f"Label batch failed for '{label_name}' on {blob_name}: {e}")
                    decisions = None
                if decisions is None:
                    failed_labels.add(label_name)
                    continue
                for seg_id, decision in decisions.items():
                    results_by_segment[seg_id][label_name] = decision

        all_docs = _build_docs(
            segments, video_id, always_strip, existing_index, results_by_segment,
            overrides_by_segment, id_to_name,
        )

        for i in range(0, len(all_docs), INDEX_BATCH_SIZE):
            _index_documents(all_docs[i:i + INDEX_BATCH_SIZE])

        logging.info(f"Labeled {len(all_docs)} segments for {blob_name}")

    except Exception as e:
        logging.exception(f"Failed to process {blob_name}: {e}")
        failed_labels = {d["name"] for d in label_defs}
    finally:
        try:
            mark_video_done(labels_container, round_id, blob_name, failed_labels)
            try_finish_round(labels_container, round_id)
        except Exception as e:
            # Re-raise so the queue redelivers this message and tries again —
            # marking a video done is idempotent (same path, overwrite), so a
            # retry can't double-count or corrupt anything.
            logging.exception(f"Failed to record progress for {blob_name}: {e}")
            raise
