"""
shared/labeling_queue.py - Shared logic for starting and tracking AI labeling runs

Used by both ManageLabels (whenever a label is added/edited/deactivated/reset)
and LabelSegments (to record each video's progress and to automatically chain
the next run once the current one finishes). Ensures only one labeling run is
in flight at a time: if a run is already "running", enqueue_labeling_job is a
no-op — whatever labels/removals triggered the call simply stay pending
(applied=False / still in removed_labels) until the current run completes.

Each run is either library-wide (pending labels against every video) or
targeted (every active label against one video, or only unlabeled videos).
Only a library-wide run marks labels applied/incomplete or clears
removed_labels when it finishes, since a targeted run hasn't covered every video.

Progress is tracked with one marker blob per video per run
(labels/progress/<round_id>/<blob_name>) rather than a shared counter, so
videos never write to the same place. Closing out a finished run is guarded
by a blob lease so it happens exactly once.
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient

SEARCH_API_VERSION = "2024-05-01-preview"


def _blob_service() -> BlobServiceClient:
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    key = os.environ["AZURE_STORAGE_KEY"]
    return BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=key,
    )


def _claim_run(status_bc, label_names: List[str], strip_names: List[str], total: int, scope: str) -> Optional[str]:
    """Atomically check no run is in progress, and if so, claim one.

    Returns the new round's id (caller should enqueue messages stamped with
    it) if this call claimed the run, or None if a run is already in
    progress (nothing to do).
    """
    try:
        status_bc.upload_blob(json.dumps({"status": "idle"}), overwrite=False)
    except Exception:
        pass  # blob already exists

    lease = None
    for attempt in range(5):
        try:
            lease = status_bc.acquire_lease(lease_duration=15)
            break
        except Exception:
            if attempt == 4:
                raise
            time.sleep(1 + attempt)

    try:
        try:
            current = json.loads(status_bc.download_blob(lease=lease).readall())
        except Exception:
            current = {}

        if current.get("status") == "running":
            return None

        round_id = str(uuid.uuid4())
        status = {
            "status": "running",
            "round_id": round_id,
            "total": total,
            "completed": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "label_names": label_names,
            "strip_names": strip_names,
            "scope": scope,
        }
        status_bc.upload_blob(json.dumps(status, ensure_ascii=False), overwrite=True, lease=lease)
        return round_id
    finally:
        try:
            lease.release()
        except Exception:
            pass


def _start_round(label_defs: List[Dict], strip_names: List[str], blob_names: List[str], scope: str) -> bool:
    total = len(blob_names)
    if total == 0:
        return False

    service = _blob_service()
    labels_container = os.environ.get("LABELS_CONTAINER", "labels")
    status_bc = service.get_blob_client(container=labels_container, blob="labeling_status.json")

    label_names = [d["name"] for d in label_defs]
    round_id = _claim_run(status_bc, label_names, strip_names, total, scope)
    if round_id is None:
        return False  # a run is already in progress; picked up automatically when it finishes

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    key = os.environ["AZURE_STORAGE_KEY"]
    queue_name = os.environ.get("LABEL_QUEUE_NAME", "label-jobs")

    queue_client = QueueClient(
        account_url=f"https://{account}.queue.core.windows.net",
        queue_name=queue_name,
        credential=key,
    )
    try:
        queue_client.create_queue()
    except Exception:
        pass  # Already exists

    for blob_name in blob_names:
        message = json.dumps({
            "blob_name": blob_name,
            "round_id": round_id,
            "label_defs": label_defs,
            "strip_names": strip_names,
        })
        queue_client.send_message(message)

    return True


def enqueue_labeling_job(library: Dict[str, Any]) -> None:
    """List all segment blobs and enqueue one message per video for this labeling round.

    No-ops if a labeling run is already in progress; the labels/removals that
    triggered this call stay pending and get picked up automatically once the
    current run finishes (see try_finish_round).
    """
    all_labels = library.get("labels", [])
    active_labels = [l for l in all_labels if l.get("is_active", True)]
    unapplied_labels = [l for l in active_labels if not l.get("applied", False)]
    removed_label_names = set(library.get("removed_labels", []))

    if not unapplied_labels and not removed_label_names:
        return

    label_defs = [{"name": l["name"], "description": l["description"], "examples": l.get("examples", [])} for l in unapplied_labels]
    strip_names = list({l["name"] for l in unapplied_labels} | removed_label_names)

    service = _blob_service()
    segments_container = os.environ.get("SEGMENTS_CONTAINER", "segments")
    cc = service.get_container_client(segments_container)
    blob_names = [b.name for b in cc.list_blobs() if b.name.endswith(".json")]

    _start_round(label_defs, strip_names, blob_names, "library")


def _find_unlabeled_video_blobs() -> List[str]:
    service = _blob_service()
    segments_container = os.environ.get("SEGMENTS_CONTAINER", "segments")
    cc = service.get_container_client(segments_container)
    all_video_ids = {b.name[:-5] for b in cc.list_blobs() if b.name.endswith(".json")}
    if not all_video_ids:
        return []

    endpoint = os.environ["SEARCH_ENDPOINT"].rstrip("/")
    admin_key = os.environ["SEARCH_ADMIN_KEY"]
    index_name = os.environ.get("SEARCH_INDEX", "segments")
    url = f"{endpoint}/indexes/{index_name}/docs/search?api-version={SEARCH_API_VERSION}"
    headers = {"Content-Type": "application/json", "api-key": admin_key}

    labeled_video_ids = set()
    skip = 0
    page_size = 1000
    while True:
        body = {
            "search": "*",
            "select": "video_id,pred_label_details",
            "top": page_size,
            "skip": skip,
            "orderby": "video_id asc, start_ms asc",
        }
        r = requests.post(url, headers=headers, json=body, timeout=60)
        r.raise_for_status()
        docs = r.json().get("value", [])
        if not docs:
            break
        for doc in docs:
            raw = doc.get("pred_label_details")
            if raw and raw != "[]" and doc.get("video_id"):
                labeled_video_ids.add(doc["video_id"])
        if len(docs) < page_size:
            break
        skip += page_size

    return [f"{vid}.json" for vid in (all_video_ids - labeled_video_ids)]


def enqueue_unlabeled_videos_job(library: Dict[str, Any]) -> Dict[str, Any]:
    active_labels = [l for l in library.get("labels", []) if l.get("is_active", True)]
    blob_names = _find_unlabeled_video_blobs() if active_labels else []
    if not blob_names:
        return {"found": 0, "started": False}

    label_defs = [{"name": l["name"], "description": l["description"], "examples": l.get("examples", [])} for l in active_labels]
    started = _start_round(label_defs, [], blob_names, "targeted")
    return {"found": len(blob_names), "started": started}


def enqueue_single_video_job(library: Dict[str, Any], video_id: str) -> bool:
    active_labels = [l for l in library.get("labels", []) if l.get("is_active", True)]
    if not active_labels:
        return False
    label_defs = [{"name": l["name"], "description": l["description"], "examples": l.get("examples", [])} for l in active_labels]
    return _start_round(label_defs, [], [f"{video_id}.json"], "targeted")


def mark_video_done(labels_container: str, round_id: str, blob_name: str, failed_labels=()) -> None:
    """Mark one video done for this round. Each video writes to its own unique
    path, so this can never conflict with any other video's write. The marker
    records which labels had failed AI calls for this video."""
    service = _blob_service()
    bc = service.get_blob_client(container=labels_container, blob=f"progress/{round_id}/{blob_name}")
    bc.upload_blob(json.dumps({"failed_labels": sorted(failed_labels)}).encode(), overwrite=True)


def count_done(labels_container: str, round_id: str) -> int:
    service = _blob_service()
    cc = service.get_container_client(labels_container)
    return sum(1 for _ in cc.list_blobs(name_starts_with=f"progress/{round_id}/"))


def _failed_labels(service: BlobServiceClient, labels_container: str, round_id: str) -> set:
    cc = service.get_container_client(labels_container)
    failed = set()
    for b in cc.list_blobs(name_starts_with=f"progress/{round_id}/"):
        raw = service.get_blob_client(container=labels_container, blob=b.name).download_blob().readall()
        try:
            failed.update(json.loads(raw).get("failed_labels", []))
        except (ValueError, AttributeError):
            pass
    return failed


def try_finish_round(labels_container: str, round_id: str) -> None:
    service = _blob_service()
    status_bc = service.get_blob_client(container=labels_container, blob="labeling_status.json")
    status = json.loads(status_bc.download_blob().readall())

    if status.get("round_id") != round_id or status.get("status") != "running":
        return

    if count_done(labels_container, round_id) < status["total"]:
        return

    failed_labels = _failed_labels(service, labels_container, round_id)
    if failed_labels:
        logging.warning(f"Round {round_id} finished with failed AI calls for labels: {sorted(failed_labels)}")

    lease = None
    for attempt in range(5):
        try:
            lease = status_bc.acquire_lease(lease_duration=15)
            break
        except Exception:
            if attempt == 4:
                return
            time.sleep(1 + attempt)

    try:
        status = json.loads(status_bc.download_blob(lease=lease).readall())
        if status.get("round_id") != round_id or status.get("status") != "running":
            return 

        status["status"] = "complete"
        status["completed"] = status["total"]
        status["failed_labels"] = sorted(failed_labels)
        status_bc.upload_blob(json.dumps(status, ensure_ascii=False), overwrite=True, lease=lease)

        label_bc = service.get_blob_client(container=labels_container, blob="label_library.json")
        library = json.loads(label_bc.download_blob().readall())
        if status.get("scope", "library") == "library":
            applied_names = set(status.get("label_names", []))
            stripped_names = set(status.get("strip_names", []))
            for l in library.get("labels", []):
                if l["name"] in applied_names:
                    l["applied"] = True
                    l["incomplete"] = l["name"] in failed_labels
            library["removed_labels"] = [n for n in library.get("removed_labels", []) if n not in stripped_names]
            label_bc.upload_blob(json.dumps(library, ensure_ascii=False, indent=2), overwrite=True)
    finally:
        try:
            lease.release()
        except Exception:
            pass

    cc = service.get_container_client(labels_container)
    for b in cc.list_blobs(name_starts_with=f"progress/{round_id}/"):
        service.get_blob_client(container=labels_container, blob=b.name).delete_blob()

    enqueue_labeling_job(library)
