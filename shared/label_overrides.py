"""
shared/label_overrides.py - Manual add/remove overrides for AI-predicted labels

Lets a reviewer force a label on or off for one segment, on top of whatever
the AI decided. Used by LabelOverride (to record an override and patch the
search index immediately) and by LabelSegments (to re-apply every standing
override each time it recomputes a video's labels, so a manual edit keeps
holding on every future labeling run, not just the one right after it's made).

Overrides are stored in an Azure Table (one row per video+segment+label,
keyed by the label's permanent id rather than its name, so renaming a label
later can't orphan the override) and are never allowed to touch the AI's own
record of what it decided (pred_label_details) — only the derived, visible
label list (pred_labels) is affected. That's what makes "undo" free: clear an
override and the visible list is just recomputed straight from the AI's
record again, no separate rollback needed.

Environment Variables:
  AZURE_STORAGE_ACCOUNT   - Storage account name
  AZURE_STORAGE_KEY       - Storage account key
"""

import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List

from azure.core.credentials import AzureNamedKeyCredential
from azure.core.exceptions import ResourceExistsError
from azure.data.tables import TableClient, TableServiceClient

TABLE_NAME = "LabelOverrides"


def _table_client() -> TableClient:
    """Connect to the LabelOverrides table, creating it on first use."""
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    key = os.environ["AZURE_STORAGE_KEY"]
    service = TableServiceClient(
        endpoint=f"https://{account}.table.core.windows.net",
        credential=AzureNamedKeyCredential(account, key),
    )
    try:
        service.create_table(TABLE_NAME)
    except ResourceExistsError:
        pass
    return service.get_table_client(TABLE_NAME)


def set_override(
    video_id: str,
    segment_id: str,
    label_id: str,
    action: str,
    reasoning: str,
    label_name: str,
    label_description: str,
) -> None:
    """Record a manual add/remove for one segment+label.

    Overwrites whatever override already existed for that same segment+label
    (there's only ever one row per pair, holding its latest state) — so
    flipping a label back just replaces the row, never leaves a stale one
    behind. label_name/label_description are snapshotted at the time of this
    edit, so it stays clear what the label meant even if it's since been
    renamed or redescribed.
    """
    table = _table_client()
    entity = {
        "PartitionKey": video_id,
        "RowKey": f"{segment_id}_{label_id}",
        "segment_id": segment_id,
        "label_id": label_id,
        "label_name": label_name,
        "label_description": label_description,
        "action": action,
        "reasoning": reasoning,
        "set_at": datetime.now(timezone.utc).isoformat(),
    }
    table.upsert_entity(entity)


def get_overrides_for_video(video_id: str) -> Dict[str, Dict[str, Dict]]:
    """Fetch every manual override recorded for this video.

    Returns {segment_id: {label_id: {action, reasoning, label_name,
    label_description, set_at}}}.
    """
    table = _table_client()
    escaped = video_id.replace("'", "''")
    overrides: Dict[str, Dict[str, Dict]] = defaultdict(dict)
    for entity in table.query_entities(f"PartitionKey eq '{escaped}'"):
        overrides[entity["segment_id"]][entity["label_id"]] = {
            "action": entity["action"],
            "reasoning": entity.get("reasoning", ""),
            "label_name": entity.get("label_name", ""),
            "label_description": entity.get("label_description", ""),
            "set_at": entity.get("set_at"),
        }
    return overrides


def clear_overrides_for_segment(video_id: str, segment_id: str) -> int:
    """Delete every override recorded for one segment (all labels), so its
    visible list falls back to exactly what the AI's own record says.
    Returns how many rows were cleared.
    """
    table = _table_client()
    escaped_video = video_id.replace("'", "''")
    prefix = segment_id.replace("'", "''") + "_"
    filter_query = f"PartitionKey eq '{escaped_video}' and RowKey ge '{prefix}' and RowKey lt '{prefix}~'"
    count = 0
    for entity in table.query_entities(filter_query):
        table.delete_entity(partition_key=entity["PartitionKey"], row_key=entity["RowKey"])
        count += 1
    return count


def apply_overrides(
    pred_label_details: List[Dict],
    overrides: Dict[str, Dict],
    id_to_name: Dict[str, str],
) -> List[str]:
    """Compute a segment's visible label list: the AI's applied=true labels,
    minus any manually removed, plus any manually added.

    pred_label_details must already have deleted/renamed labels stripped out
    (LabelSegments does this before calling in); this function doesn't filter
    that itself. An override whose label_id isn't in id_to_name (the label
    was deleted or deactivated since the override was set) is silently
    ignored, so a stale override can never resurrect a label that no longer
    exists.
    """
    ai_applied = {d["name"] for d in pred_label_details if isinstance(d, dict) and d.get("applied")}
    removed = set()
    added = set()
    for label_id, override in overrides.items():
        name = id_to_name.get(label_id)
        if not name:
            continue
        if override["action"] == "remove":
            removed.add(name)
        elif override["action"] == "add":
            added.add(name)
    return sorted((ai_applied - removed) | added)
