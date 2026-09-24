import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List

from azure.core.credentials import AzureNamedKeyCredential
from azure.core.exceptions import ResourceExistsError
from azure.data.tables import TableClient, TableServiceClient

TABLE_NAME = "LabelOverrides"


def _table_client() -> TableClient:
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


def apply_overrides(
    pred_label_details: List[Dict],
    overrides: Dict[str, Dict],
    id_to_name: Dict[str, str],
) -> List[str]:
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
