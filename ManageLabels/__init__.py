"""
ManageLabels - Azure Function for Label Library Management

This Azure Function manages label definitions for AI-powered segment labeling:
1. Stores label definitions in Azure Blob Storage (labels/label_library.json)
2. Supports CRUD operations (Create, Read, Update, Delete)
3. Validates label uniqueness and format

Architecture Role:
- Provides label management for the annotation system
- Accessed by UI for label CRUD operations
- Read by future function for AI labeling

Input: HTTP request (GET/POST/PUT/DELETE)
Output: JSON with label data or operation status
"""

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import azure.functions as func
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient


def _blob_service() -> BlobServiceClient:
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    key = os.environ["AZURE_STORAGE_KEY"]
    return BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=key,
    )


def _read_label_json() -> Dict[str, Any]:
    try:
        service = _blob_service()
        container = os.environ.get("LABELS_CONTAINER", "labels")
        bc = service.get_blob_client(container=container, blob="label_library.json")
        data = bc.download_blob().readall()
        return json.loads(data)
    except Exception:
        # Return empty library if not found
        return {
            "labels": [],
            "last_updated": datetime.now(timezone.utc).isoformat()
        }


def _write_label_json(library: Dict[str, Any]) -> str:
    service = _blob_service()
    container = os.environ.get("LABELS_CONTAINER", "labels")

    # Create container if needed
    try:
        service.get_container_client(container).create_container()
    except Exception:
        pass

    bc = service.get_blob_client(container=container, blob="label_library.json")
    bc.upload_blob(json.dumps(library, ensure_ascii=False, indent=2), overwrite=True)
    return f"{container}/label_library.json"


def _read_labeling_status() -> Optional[Dict]:
    try:
        service = _blob_service()
        container = os.environ.get("LABELS_CONTAINER", "labels")
        bc = service.get_blob_client(container=container, blob="labeling_status.json")
        return json.loads(bc.download_blob().readall())
    except Exception:
        return None


def _start_labeling_job(library: Dict[str, Any]) -> None:
    try:
        _enqueue_labeling_job(library)
    except Exception as e:
        logging.exception(f"Failed to enqueue labeling job: {e}")


def _enqueue_labeling_job(library: Dict[str, Any]) -> None:
    """List all segment blobs, write a status blob, and enqueue one message per video."""
    all_labels = library.get("labels", [])
    active_labels = [l for l in all_labels if l.get("is_active", True)]
    unapplied_labels = [l for l in active_labels if not l.get("applied", False)]
    removed_label_names = set(library.get("removed_labels", []))

    if not unapplied_labels and not removed_label_names:
        return

    label_defs = [{"name": l["name"], "description": l["description"], "examples": l.get("examples", [])} for l in unapplied_labels]
    valid_names = [l["name"] for l in unapplied_labels]
    strip_names = list({l["name"] for l in unapplied_labels} | removed_label_names)

    service = _blob_service()
    segments_container = os.environ.get("SEGMENTS_CONTAINER", "segments")
    cc = service.get_container_client(segments_container)
    blob_names = [b.name for b in cc.list_blobs() if b.name.endswith(".json")]
    total = len(blob_names)

    if total == 0:
        return

    labels_container = os.environ.get("LABELS_CONTAINER", "labels")
    status = {
        "status": "running",
        "total": total,
        "completed": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    status_bc = service.get_blob_client(container=labels_container, blob="labeling_status.json")
    status_bc.upload_blob(json.dumps(status, ensure_ascii=False), overwrite=True)

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
            "label_defs": label_defs,
            "valid_names": valid_names,
            "strip_names": strip_names,
            "total": total,
        })
        queue_client.send_message(message)


def _validate_label_name(name: str, library: Dict[str, Any], exclude_id: Optional[str] = None) -> bool:
    """Check if label name is unique among active labels"""
    for label in library.get("labels", []):
        if label["label_id"] == exclude_id:
            continue
        if label["name"].lower() == name.lower() and label.get("is_active", True):
            return False
    return True


def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        method = req.method.upper()

        if method == "GET":
            library = _read_label_json()
            # Filter to active labels only (unless query param says otherwise)
            include_inactive = req.params.get("include_inactive", "false").lower() == "true"
            if not include_inactive:
                library["labels"] = [l for l in library["labels"] if l.get("is_active", True)]

            status = _read_labeling_status()
            if status:
                library["labeling_status"] = status

            return func.HttpResponse(
                json.dumps(library, ensure_ascii=False),
                mimetype="application/json",
                status_code=200,
            )

        # POST/PUT/DELETE/PATCH: Modify labels
        try:
            body = req.get_json()
        except Exception:
            body = {}
        library = _read_label_json()

        # POST: Add new label
        if method == "POST":
            name = body.get("name", "").strip()
            description = body.get("description", "").strip()

            if not name:
                return func.HttpResponse(
                    json.dumps({"error": "Label 'name' is required"}),
                    mimetype="application/json",
                    status_code=400,
                )

            if not _validate_label_name(name, library):
                return func.HttpResponse(
                    json.dumps({"error": f"Label '{name}' already exists"}),
                    mimetype="application/json",
                    status_code=400,
                )

            now = datetime.now(timezone.utc).isoformat()
            examples = [e.strip() for e in body.get("examples", []) if e.strip()][:3]
            new_label = {
                "label_id": str(uuid.uuid4()),
                "name": name,
                "description": description,
                "examples": examples,
                "created_at": now,
                "updated_at": now,
                "is_active": True,
                "applied": False,
            }

            library["labels"].append(new_label)
            library["last_updated"] = now
            _write_label_json(library)
            threading.Thread(target=_start_labeling_job, args=(library,), daemon=True).start()

            return func.HttpResponse(
                json.dumps(new_label, ensure_ascii=False),
                mimetype="application/json",
                status_code=201,
            )

        # PUT: Update existing label
        elif method == "PUT":
            label_id = body.get("label_id")
            if not label_id:
                return func.HttpResponse(
                    json.dumps({"error": "'label_id' is required for update"}),
                    mimetype="application/json",
                    status_code=400,
                )

            label = next((l for l in library["labels"] if l["label_id"] == label_id), None)
            if not label:
                return func.HttpResponse(
                    json.dumps({"error": f"Label '{label_id}' not found"}),
                    mimetype="application/json",
                    status_code=404,
                )

            # Update fields
            old_name = label["name"]
            if "name" in body:
                new_name = body["name"].strip()
                if new_name and not _validate_label_name(new_name, library, exclude_id=label_id):
                    return func.HttpResponse(
                        json.dumps({"error": f"Label '{new_name}' already exists"}),
                        mimetype="application/json",
                        status_code=400,
                    )
                label["name"] = new_name
                # Track old name so LabelSegments can strip it from existing assignments
                if old_name != new_name:
                    removed = library.setdefault("removed_labels", [])
                    if old_name not in removed:
                        removed.append(old_name)

            if "description" in body:
                label["description"] = body["description"].strip()

            if "examples" in body:
                label["examples"] = [e.strip() for e in body["examples"] if e.strip()][:3]

            if "is_active" in body:
                label["is_active"] = bool(body["is_active"])

            # Reset applied so LabelSegments re-runs this label against all segments
            label["applied"] = False
            label["updated_at"] = datetime.now(timezone.utc).isoformat()
            library["last_updated"] = label["updated_at"]
            _write_label_json(library)
            threading.Thread(target=_start_labeling_job, args=(library,), daemon=True).start()

            return func.HttpResponse(
                json.dumps(label, ensure_ascii=False),
                mimetype="application/json",
                status_code=200,
            )

        # DELETE: Soft delete label
        elif method == "DELETE":
            label_id = body.get("label_id")
            if not label_id:
                return func.HttpResponse(
                    json.dumps({"error": "'label_id' is required for delete"}),
                    mimetype="application/json",
                    status_code=400,
                )

            label = next((l for l in library["labels"] if l["label_id"] == label_id), None)
            if not label:
                return func.HttpResponse(
                    json.dumps({"error": f"Label '{label_id}' not found"}),
                    mimetype="application/json",
                    status_code=404,
                )

            label["is_active"] = False
            # Track name so LabelSegments strips this label from all segment assignments
            removed = library.setdefault("removed_labels", [])
            if label["name"] not in removed:
                removed.append(label["name"])
            label["updated_at"] = datetime.now(timezone.utc).isoformat()
            library["last_updated"] = label["updated_at"]
            _write_label_json(library)
            threading.Thread(target=_start_labeling_job, args=(library,), daemon=True).start()

            return func.HttpResponse(
                json.dumps({"success": True, "message": "Label deactivated"}),
                mimetype="application/json",
                status_code=200,
            )

        # PATCH: Reset all labels to force a full re-run
        elif method == "PATCH":
            now = datetime.now(timezone.utc).isoformat()
            count = 0
            for label in library["labels"]:
                if label.get("is_active", True):
                    label["applied"] = False
                    count += 1

            # Re-queue every inactive label for stripping too - otherwise a label
            # only gets removed from segments on the one run right after it's
            # deactivated, and any later full re-run silently leaves it in place.
            inactive_names = {l["name"] for l in library["labels"] if not l.get("is_active", True)}
            if inactive_names:
                removed = library.setdefault("removed_labels", [])
                for name in inactive_names:
                    if name not in removed:
                        removed.append(name)

            library["last_updated"] = now
            _write_label_json(library)
            threading.Thread(target=_start_labeling_job, args=(library,), daemon=True).start()

            return func.HttpResponse(
                json.dumps({"message": f"Reset {count} labels, re-labeling queued."}),
                mimetype="application/json",
                status_code=200,
            )

        else:
            return func.HttpResponse(
                json.dumps({"error": f"Unsupported HTTP method '{method}'"}),
                mimetype="application/json",
                status_code=405,
            )

    except Exception as e:
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            mimetype="application/json",
            status_code=500,
        )
