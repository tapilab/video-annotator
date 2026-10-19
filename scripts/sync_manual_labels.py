"""
scripts/sync_manual_labels.py - Copy manual label reasons from the LabelOverrides table into the search index

For backfills and repairs; never creates segments. Settings come from local.settings.json.
Run --apply when nobody is editing labels.

Usage:
  python scripts/sync_manual_labels.py           # dry run, writes nothing
  python scripts/sync_manual_labels.py --apply   # write the changes
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import requests
from azure.core.credentials import AzureNamedKeyCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient
from azure.storage.blob import BlobServiceClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from shared.label_overrides import TABLE_NAME, manual_details_for_index

SEARCH_API_VERSION = "2024-05-01-preview"
PAGE_SIZE = 1000
SAMPLE_LIMIT = 20


def _load_settings() -> None:
    """Fill in any settings not already set from local.settings.json (the backend's local config)."""
    path = os.path.join(ROOT, "local.settings.json")
    if not os.path.exists(path):
        return
    with open(path) as f:
        values = json.load(f).get("Values", {})
    for key, value in values.items():
        os.environ.setdefault(key, str(value))


def _read_label_names() -> Dict[str, str]:
    """Return {label_id: current name} for active labels."""
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    service = BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=os.environ["AZURE_STORAGE_KEY"],
    )
    container = os.environ.get("LABELS_CONTAINER", "labels")
    blob = service.get_blob_client(container=container, blob="label_library.json")
    library = json.loads(blob.download_blob().readall())
    return {l["label_id"]: l["name"] for l in library.get("labels", []) if l.get("is_active", True)}


def _read_all_overrides() -> Tuple[Dict[str, Dict[str, Dict]], List[str]]:
    """Return every override grouped by segment_key, plus a list of rows saved without a reason."""
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    table = TableClient(
        endpoint=f"https://{account}.table.core.windows.net",
        table_name=TABLE_NAME,
        credential=AzureNamedKeyCredential(account, os.environ["AZURE_STORAGE_KEY"]),
    )
    try:
        entities = list(table.list_entities())
    except ResourceNotFoundError:
        sys.exit(f"No '{TABLE_NAME}' table in storage account '{account}' - check the settings.")

    by_segment: Dict[str, Dict[str, Dict]] = defaultdict(dict)
    missing_reason = []
    for entity in entities:
        segment_key = f"{entity['PartitionKey']}_{entity['segment_id']}"
        by_segment[segment_key][entity["label_id"]] = {
            "action": entity["action"],
            "reasoning": entity.get("reasoning", ""),
        }
        if not (entity.get("reasoning") or "").strip():
            missing_reason.append(f"{segment_key} / {entity.get('label_name') or entity['label_id']}")
    return by_segment, missing_reason


def _search_request(path: str, body: Dict) -> Dict:
    endpoint = os.environ["SEARCH_ENDPOINT"].rstrip("/")
    index_name = os.environ.get("SEARCH_INDEX", "segments")
    r = requests.post(
        f"{endpoint}/indexes/{index_name}/docs/{path}?api-version={SEARCH_API_VERSION}",
        headers={"Content-Type": "application/json", "api-key": os.environ["SEARCH_ADMIN_KEY"]},
        json=body,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def _read_index_details() -> Dict[str, Optional[str]]:
    """Return {segment_key: current manual_label_details} for every segment in the index."""
    current: Dict[str, Optional[str]] = {}
    skip = 0
    while True:
        body = {
            "search": "*",
            "select": "segment_key,manual_label_details",
            "orderby": "video_id asc, segment_id asc",
            "top": PAGE_SIZE,
            "skip": skip,
            "count": True,
        }
        try:
            data = _search_request("search", body)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 400 and "manual_label_details" in e.response.text:
                sys.exit("The search index has no 'manual_label_details' field yet. Add it to the index first.")
            raise
        if skip == 0 and (data.get("@odata.count") or 0) > 100_000:
            sys.exit("The index has more than 100,000 segments, which this script can't page through.")
        docs = data.get("value", [])
        for doc in docs:
            current[doc["segment_key"]] = doc.get("manual_label_details")
        if len(docs) < PAGE_SIZE:
            break
        skip += len(docs)
    return current


def _plan_changes(
    expected: Dict[str, str], current: Dict[str, Optional[str]]
) -> Tuple[List[Tuple[str, Optional[str], str]], List[str]]:
    """Return ([(segment_key, current value, new value)] to write, [segment_keys with edits but not in the index])."""
    changes = []
    for segment_key, now in current.items():
        want = expected.get(segment_key, "[]")
        if want == "[]" and now in (None, "[]"):
            continue
        if now != want:
            changes.append((segment_key, now, want))
    not_in_index = sorted(k for k in expected if k not in current)
    return changes, not_in_index


def _apply_changes(changes: List[Tuple[str, Optional[str], str]]) -> List[Dict]:
    """Write the new values (merge only — never creates segments). Return any per-segment failures."""
    failures = []
    for i in range(0, len(changes), PAGE_SIZE):
        batch = changes[i:i + PAGE_SIZE]
        body = {"value": [
            {"@search.action": "merge", "segment_key": key, "manual_label_details": want}
            for key, _, want in batch
        ]}
        result = _search_request("index", body)
        failures.extend(v for v in result.get("value", []) if not v.get("status", False))
    return failures


def _print_list(title: str, items: List[str]) -> None:
    print(f"\n{title}: {len(items)}")
    for item in items[:SAMPLE_LIMIT]:
        print(f"  - {item}")
    if len(items) > SAMPLE_LIMIT:
        print(f"  ... and {len(items) - SAMPLE_LIMIT} more")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync manual label reasons from the LabelOverrides table into the search index.")
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run, report only)")
    args = parser.parse_args()

    _load_settings()
    id_to_name = _read_label_names()
    overrides, missing_reason = _read_all_overrides()
    expected = {key: manual_details_for_index(ovs, id_to_name) for key, ovs in overrides.items()}
    inactive = sorted(
        f"{key} / {label_id}" for key, ovs in overrides.items() for label_id in ovs if label_id not in id_to_name
    )
    current = _read_index_details()
    changes, not_in_index = _plan_changes(expected, current)

    fills = [f"{key}: {want}" for key, now, want in changes if want != "[]"]
    clears = [f"{key}: {now}" for key, now, want in changes if want == "[]"]

    print(f"Manual edits in table: {sum(len(ovs) for ovs in overrides.values())} across {len(overrides)} segments")
    print(f"Segments in search index: {len(current)}")
    _print_list("Segments to fill/update", fills)
    _print_list("Segments to clear (reasons in index, no edits in table)", clears)
    _print_list("Edits for segments not in the index (left alone)", not_in_index)
    _print_list("Edits for deactivated/deleted labels (skipped)", inactive)
    _print_list("Edits saved without a reason", missing_reason)

    if not args.apply:
        print("\nDry run - nothing was written. Re-run with --apply to write these changes.")
        return
    if not changes:
        print("\nNothing to write - the index is already in sync.")
        return

    failures = _apply_changes(changes)
    print(f"\nWrote {len(changes) - len(failures)} of {len(changes)} segments.")
    if failures:
        _print_list("Failed writes", [f"{f.get('key')}: {f.get('errorMessage')}" for f in failures])
        sys.exit(1)


if __name__ == "__main__":
    main()
