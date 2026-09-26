"""
LabelOverride - Azure Function for manually adding/removing a label on one segment

Lets a reviewer override the AI's labeling for a single segment: force a
label on that the AI didn't apply, or force one off that it did. Records the
override (see shared/label_overrides.py) and immediately patches that one
segment's visible labels in the search index — no waiting for a labeling
round. The AI's own record of what it decided (pred_label_details) is never
touched, only the derived pred_labels list, which is why an override can
always be reversed later without needing a fresh AI run.

Every override made here is re-applied by LabelSegments on every future
labeling run for that video, so it isn't undone the next time labels change.

Input: POST with:
  {
    "video_id": "...", "segment_id": "...", "label_id": "<label's permanent id>",
    "action": "add" | "remove", "reasoning": "..." (required, never blank)
  }

Output: JSON with the segment's updated {segment_key, pred_labels}, or an
error (400 for bad input or an inactive/unknown label_id, 404 if the segment
isn't in the search index).

Environment Variables:
  AZURE_STORAGE_ACCOUNT   - Storage account name
  AZURE_STORAGE_KEY       - Storage account key
  LABELS_CONTAINER        - Blob container for label library (default: "labels")
  SEARCH_ENDPOINT         - Azure AI Search endpoint
  SEARCH_ADMIN_KEY        - Azure AI Search admin key
  SEARCH_INDEX            - Search index name (default: "segments")
"""

import json
import os
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import azure.functions as func
import requests
from azure.storage.blob import BlobServiceClient

from shared.label_overrides import apply_overrides, get_overrides_for_video, set_override

SEARCH_API_VERSION = "2024-05-01-preview"


def _blob_service() -> BlobServiceClient:
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    key = os.environ["AZURE_STORAGE_KEY"]
    return BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=key,
    )


def _read_label_library() -> Dict[str, Any]:
    service = _blob_service()
    container = os.environ.get("LABELS_CONTAINER", "labels")
    bc = service.get_blob_client(container=container, blob="label_library.json")
    return json.loads(bc.download_blob().readall())


def _find_active_label(library: Dict[str, Any], label_id: str) -> Optional[Dict]:
    """Look up a label by its permanent id, among active labels only."""
    for label in library.get("labels", []):
        if label["label_id"] == label_id and label.get("is_active", True):
            return label
    return None


def _fetch_segment_doc(segment_key: str) -> Optional[Dict[str, Any]]:
    """Look up one segment's current pred_labels/pred_label_details by key.
    Returns None if the segment isn't in the search index."""
    endpoint = os.environ["SEARCH_ENDPOINT"].rstrip("/")
    admin_key = os.environ["SEARCH_ADMIN_KEY"]
    index_name = os.environ.get("SEARCH_INDEX", "segments")
    url = (
        f"{endpoint}/indexes/{index_name}/docs/{quote(segment_key, safe='')}"
        f"?api-version={SEARCH_API_VERSION}&$select=segment_key,pred_labels,pred_label_details"
    )
    r = requests.get(url, headers={"api-key": admin_key}, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _patch_segment_labels(segment_key: str, pred_labels: List[str]) -> None:
    endpoint = os.environ["SEARCH_ENDPOINT"].rstrip("/")
    admin_key = os.environ["SEARCH_ADMIN_KEY"]
    index_name = os.environ.get("SEARCH_INDEX", "segments")
    url = f"{endpoint}/indexes/{index_name}/docs/index?api-version={SEARCH_API_VERSION}"
    body = {"value": [{"@search.action": "mergeOrUpload", "segment_key": segment_key, "pred_labels": pred_labels}]}
    r = requests.post(
        url,
        headers={"Content-Type": "application/json", "api-key": admin_key},
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    failed = [v for v in r.json().get("value", []) if not v.get("succeeded", True)]
    if failed:
        raise RuntimeError(f"Search indexing failed: {failed[:1]}")


def main(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "GET":
        video_id = (req.params.get("video_id") or "").strip()
        if not video_id:
            return func.HttpResponse(
                json.dumps({"error": "'video_id' query parameter is required"}),
                mimetype="application/json",
                status_code=400,
            )
        try:
            overrides = get_overrides_for_video(video_id)
            return func.HttpResponse(
                json.dumps(overrides, ensure_ascii=False),
                mimetype="application/json",
                status_code=200,
            )
        except Exception as e:
            return func.HttpResponse(
                json.dumps({"error": str(e)}),
                mimetype="application/json",
                status_code=500,
            )

    try:
        body = req.get_json()
    except Exception:
        body = {}

    video_id = (body.get("video_id") or "").strip()
    segment_id = (body.get("segment_id") or "").strip()
    label_id = (body.get("label_id") or "").strip()
    action = (body.get("action") or "").strip().lower()
    reasoning = (body.get("reasoning") or "").strip()

    if not video_id or not segment_id or not label_id:
        return func.HttpResponse(
            json.dumps({"error": "'video_id', 'segment_id', and 'label_id' are required"}),
            mimetype="application/json",
            status_code=400,
        )
    if action not in ("add", "remove"):
        return func.HttpResponse(
            json.dumps({"error": "'action' must be 'add' or 'remove'"}),
            mimetype="application/json",
            status_code=400,
        )
    if not reasoning:
        return func.HttpResponse(
            json.dumps({"error": "'reasoning' is required"}),
            mimetype="application/json",
            status_code=400,
        )

    try:
        library = _read_label_library()
        label = _find_active_label(library, label_id)
        if not label:
            return func.HttpResponse(
                json.dumps({"error": f"Label '{label_id}' not found or inactive"}),
                mimetype="application/json",
                status_code=400,
            )

        set_override(
            video_id=video_id,
            segment_id=segment_id,
            label_id=label_id,
            action=action,
            reasoning=reasoning,
            label_name=label["name"],
            label_description=label.get("description", ""),
        )

        segment_key = f"{video_id}_{segment_id}"
        doc = _fetch_segment_doc(segment_key)
        if doc is None:
            return func.HttpResponse(
                json.dumps({"error": f"Segment '{segment_key}' not found in search index"}),
                mimetype="application/json",
                status_code=404,
            )

        raw_details = doc.get("pred_label_details") or "[]"
        try:
            pred_label_details = json.loads(raw_details)
            if not isinstance(pred_label_details, list):
                pred_label_details = []
        except Exception:
            pred_label_details = []

        id_to_name = {
            l["label_id"]: l["name"] for l in library.get("labels", []) if l.get("is_active", True)
        }
        segment_overrides = get_overrides_for_video(video_id).get(segment_id, {})
        new_labels = apply_overrides(pred_label_details, segment_overrides, id_to_name)

        _patch_segment_labels(segment_key, new_labels)

        return func.HttpResponse(
            json.dumps({"segment_key": segment_key, "pred_labels": new_labels}, ensure_ascii=False),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            mimetype="application/json",
            status_code=500,
        )
