import json
import os
from typing import Any, Dict

import azure.functions as func
from azure.storage.blob import BlobServiceClient

from shared.labeling_queue import enqueue_single_video_job, enqueue_unlabeled_videos_job


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


def _video_blob_exists(video_id: str) -> bool:
    service = _blob_service()
    container = os.environ.get("SEGMENTS_CONTAINER", "segments")
    return service.get_blob_client(container=container, blob=f"{video_id}.json").exists()


def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except Exception:
        body = {}

    scope = (body.get("scope") or "").strip().lower()
    if scope not in ("unlabeled", "video"):
        return func.HttpResponse(
            json.dumps({"error": "'scope' must be 'unlabeled' or 'video'"}),
            mimetype="application/json",
            status_code=400,
        )

    video_id = (body.get("video_id") or "").strip()
    if scope == "video" and not video_id:
        return func.HttpResponse(
            json.dumps({"error": "'video_id' is required when scope is 'video'"}),
            mimetype="application/json",
            status_code=400,
        )

    try:
        if scope == "video" and not _video_blob_exists(video_id):
            return func.HttpResponse(
                json.dumps({"error": f"No video found with id '{video_id}'"}),
                mimetype="application/json",
                status_code=404,
            )

        library = _read_label_library()

        if scope == "unlabeled":
            result = enqueue_unlabeled_videos_job(library)
            if result["found"] == 0:
                message = "No unlabeled videos found."
            elif result["started"]:
                message = f"Started labeling {result['found']} video(s)."
            else:
                message = (
                    f"Found {result['found']} unlabeled video(s), but a labeling run is "
                    "already in progress — try again once it finishes."
                )
            return func.HttpResponse(
                json.dumps({**result, "message": message}, ensure_ascii=False),
                mimetype="application/json",
                status_code=200,
            )

        else:  # scope == "video"
            started = enqueue_single_video_job(library, video_id)
            message = (
                f"Started re-labeling {video_id}." if started
                else "A labeling run is already in progress — try again once it finishes."
            )
            return func.HttpResponse(
                json.dumps({"started": started, "message": message}, ensure_ascii=False),
                mimetype="application/json",
                status_code=200,
            )

    except Exception as e:
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            mimetype="application/json",
            status_code=500,
        )
