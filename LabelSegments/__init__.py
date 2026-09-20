"""
LabelSegments - Queue-triggered Azure Function for AI-powered segment labeling

Triggered by a queue message (one per video) written by ManageLabels.
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
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set

import azure.functions as func
import requests
from azure.storage.blob import BlobServiceClient
from openai import OpenAI
from pydantic import BaseModel

from shared.labeling_queue import mark_video_done, try_finish_round

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", 10))
GPT_WORKERS = int(os.environ.get("GPT_WORKERS", 5))
INDEX_BATCH_SIZE = 500
SEARCH_API_VERSION = "2024-05-01-preview"


class SegmentDecision(BaseModel):
    segment_id: str
    applied: bool
    rationale: str


class LabelJudgment(BaseModel):
    results: List[SegmentDecision]


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


def _format_label(label_def: Dict) -> str:
    description = label_def["description"]
    examples = label_def.get("examples") or []
    if examples:
        description += " " + " ".join(f'For example, the {ex}.' for ex in examples)
    return f"Name: {label_def['name']}\nDescription: {description}"


def _format_segments(seg_inputs: List[Dict]) -> str:
    return "\n".join(f"[{s['segment_id']}] {s['text']}" for s in seg_inputs)


def _call_gpt(label_def: Dict, seg_inputs: List[Dict]) -> List[SegmentDecision]:
    """Call GPT-4o-mini via proxy to judge one label against a batch of segments."""
    client = OpenAI(
        base_url=os.environ["PROXY_BASE_URL"],
        api_key="unused",
        default_headers={"x-functions-key": os.environ["FUNCTION_HOST_KEY"]},
    )

    system_prompt = (
        "You are a content labeler. You will be given one label, the label's definition, and a list of video transcript "
        "segments. For every segment, decide whether the label applies. Mark true if the segment "
        "directly mentions, discusses, or is obviously related to the label topic. Mark false if the label does not "
        "mention, discuss, or if it is not related. Give a brief rationale either way. "
        "Judge every segment listed, including ones where the label clearly does not apply."
    )

    user_prompt = (
        f"LABEL\n{_format_label(label_def)}\n\n"
        f"SEGMENTS\n{_format_segments(seg_inputs)}"
    )

    resp = client.responses.parse(
        model="gpt-4o-mini",
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        text_format=LabelJudgment,
    )

    return resp.output_parsed.results


def _process_label_batch(
    label_def: Dict,
    batch: List[Dict],
    blob_name: str,
    batch_idx: int,
) -> Optional[Dict[str, Dict]]:
    """Judge one label against one batch of segments, retrying with backoff.

    Returns {segment_id: {"applied": bool, "rationale": str}} covering every
    segment in the batch, or None if it still fails (errors, or the model
    skipping segments) after all attempts.
    """
    seg_inputs = [{"segment_id": s["segment_id"], "text": s["text"]} for s in batch]
    expected_ids = {s["segment_id"] for s in batch}

    for attempt in range(4):
        try:
            decisions = _call_gpt(label_def, seg_inputs)
            result = {d.segment_id: {"applied": d.applied, "rationale": d.rationale} for d in decisions}
            missing = expected_ids - result.keys()
            if missing:
                raise ValueError(f"model skipped {len(missing)} of {len(expected_ids)} segments")
            return result
        except Exception as e:
            if attempt == 3:
                logging.warning(
                    f"GPT call failed for label '{label_def.get('name')}' on {blob_name} "
                    f"batch {batch_idx}: {e}"
                )
            else:
                time.sleep(2 * 2 ** attempt)

    return None


def _build_docs(
    segments: List[Dict],
    video_id: str,
    always_strip: Set[str],
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
        seg_decisions = results_by_segment.get(seg_id, {})
        strip = always_strip | set(seg_decisions)
        kept_labels = [n for n in existing["pred_labels"] if n not in strip]
        kept_details = [d for d in existing["pred_label_details"] if isinstance(d, dict) and d.get("name") not in strip]

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
                    decisions = None
                if decisions is None:
                    failed_labels.add(label_name)
                    continue
                for seg_id, decision in decisions.items():
                    results_by_segment[seg_id][label_name] = decision

        all_docs = _build_docs(segments, video_id, always_strip, existing_index, results_by_segment)

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
