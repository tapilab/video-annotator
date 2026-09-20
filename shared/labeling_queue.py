"""
shared/labeling_queue.py - Shared logic for starting and tracking AI labeling runs

Used by both ManageLabels (whenever a label is added/edited/deactivated/reset)
and LabelSegments (to record each video's progress and to automatically chain
the next run once the current one finishes). Ensures only one labeling run is
in flight at a time: if a run is already "running", enqueue_labeling_job is a
no-op — whatever labels/removals triggered the call simply stay pending
(applied=False / still in removed_labels) until the current run completes.

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

from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient


def _blob_service() -> BlobServiceClient:
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    key = os.environ["AZURE_STORAGE_KEY"]
    return BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=key,
    )


def _claim_run(status_bc, label_names: List[str], strip_names: List[str], total: int) -> Optional[str]:
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
        }
        status_bc.upload_blob(json.dumps(status, ensure_ascii=False), overwrite=True, lease=lease)
        return round_id
    finally:
        try:
            lease.release()
        except Exception:
            pass


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
    total = len(blob_names)

    if total == 0:
        return

    labels_container = os.environ.get("LABELS_CONTAINER", "labels")
    status_bc = service.get_blob_client(container=labels_container, blob="labeling_status.json")

    label_names = [l["name"] for l in unapplied_labels]
    round_id = _claim_run(status_bc, label_names, strip_names, total)
    if round_id is None:
        return  # a run is already in progress; picked up automatically when it finishes

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
