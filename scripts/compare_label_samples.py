"""
scripts/compare_label_samples.py - Re-judge a label sample review workbook with another model, using production batches

Settings come from local.settings.json.

Usage:
  python scripts/compare_label_samples.py IN.xlsx OUT.xlsx --dry-run   # match rows only, no GPT calls
  python scripts/compare_label_samples.py IN.xlsx OUT.xlsx             # run gpt-5-mini and write OUT.xlsx
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import copy
from typing import Dict, List, Tuple

import openpyxl
import requests
from azure.storage.blob import BlobServiceClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from shared.gpt_labeling import BATCH_SIZE, GPT_WORKERS, process_label_batch

SEARCH_API_VERSION = "2024-05-01-preview"
PAGE_SIZE = 1000


def _load_settings() -> None:
    """Fill in any settings not already set from local.settings.json (the backend's local config)."""
    path = os.path.join(ROOT, "local.settings.json")
    if not os.path.exists(path):
        return
    with open(path) as f:
        values = json.load(f).get("Values", {})
    for key, value in values.items():
        os.environ.setdefault(key, str(value))


def _blob_service() -> BlobServiceClient:
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    return BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=os.environ["AZURE_STORAGE_KEY"],
    )


def _read_json_blob(container: str, blob_name: str):
    bc = _blob_service().get_blob_client(container=container, blob=blob_name)
    return json.loads(bc.download_blob().readall())


def _read_label_defs() -> Dict[str, Dict]:
    """Return {name: label_def} for active labels, built the same way as production."""
    library = _read_json_blob(os.environ.get("LABELS_CONTAINER", "labels"), "label_library.json")
    return {
        l["name"]: {"name": l["name"], "description": l["description"], "examples": l.get("examples", [])}
        for l in library.get("labels", []) if l.get("is_active", True)
    }


def _read_index_texts() -> Dict[str, List[Tuple[str, str]]]:
    """Return {segment text: [(video_id, segment_id), ...]} for every segment in the index."""
    endpoint = os.environ["SEARCH_ENDPOINT"].rstrip("/")
    index_name = os.environ.get("SEARCH_INDEX", "segments")
    url = f"{endpoint}/indexes/{index_name}/docs/search?api-version={SEARCH_API_VERSION}"
    headers = {"Content-Type": "application/json", "api-key": os.environ["SEARCH_ADMIN_KEY"]}

    by_text: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    skip = 0
    while True:
        body = {
            "search": "*",
            "select": "video_id,segment_id,text",
            "orderby": "video_id asc, segment_id asc",
            "top": PAGE_SIZE,
            "skip": skip,
        }
        r = requests.post(url, headers=headers, json=body, timeout=60)
        r.raise_for_status()
        docs = r.json().get("value", [])
        for doc in docs:
            by_text[(doc.get("text") or "").strip()].append((doc["video_id"], doc["segment_id"]))
        if len(docs) < PAGE_SIZE:
            break
        skip += len(docs)
    return by_text


def _read_video_segments(video_id: str) -> List[Dict]:
    """Return the video's non-empty segments in blob order, as LabelSegments sees them."""
    data = _read_json_blob(os.environ.get("SEGMENTS_CONTAINER", "segments"), f"{video_id}.json")
    raw = data.get("segments", []) if isinstance(data, dict) else data
    return [s for s in raw if (s.get("text") or "").strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-judge a label sample review workbook with another model.")
    parser.add_argument("input", help="review workbook (Segment Text, Label, Reasoning, Agree?, Comments)")
    parser.add_argument("output", help="workbook to write")
    parser.add_argument("--model", default="gpt-5-mini", help="model name sent to the proxy (default: gpt-5-mini)")
    parser.add_argument("--dry-run", action="store_true", help="match rows to segments only, no GPT calls")
    args = parser.parse_args()

    _load_settings()
    src_wb = openpyxl.load_workbook(args.input)
    src = src_wb.active
    rows = [list(r) for r in src.iter_rows(min_row=2, values_only=True) if r[0]]

    label_defs = _read_label_defs()
    by_text = _read_index_texts()
    print(f"Rows: {len(rows)}   Segments in index: {sum(len(v) for v in by_text.values())}")

    # Match each row to its video + segment, then to the production batch it sits in
    problems: Dict[int, str] = {}
    row_batch: Dict[int, Tuple[str, str, int]] = {}  # row -> (label, video_id, batch start)
    row_key: Dict[int, Tuple[str, str]] = {}
    video_segments: Dict[str, List[Dict]] = {}
    for i, (text, label, *_rest) in enumerate(rows):
        matches = by_text.get(text.strip(), [])
        if label not in label_defs:
            problems[i] = f"label '{label}' not active in the label library"
            continue
        if len(matches) != 1:
            problems[i] = "segment not found in index" if not matches else f"text matches {len(matches)} segments"
            continue
        video_id, segment_id = matches[0]
        if video_id not in video_segments:
            video_segments[video_id] = _read_video_segments(video_id)
        ids = [str(s["segment_id"]) for s in video_segments[video_id]]
        if str(segment_id) not in ids:
            problems[i] = f"segment {segment_id} not in blob {video_id}.json"
            continue
        start = ids.index(str(segment_id)) // BATCH_SIZE * BATCH_SIZE
        row_batch[i] = (label, video_id, start)
        row_key[i] = (video_id, str(segment_id))

    tasks = sorted(set(row_batch.values()))
    print(f"Matched: {len(row_batch)}   Problems: {len(problems)}   GPT calls needed: {len(tasks)}")
    for i, msg in sorted(problems.items()):
        print(f"  row {i + 2}: {msg}")
    if args.dry_run:
        print("\nDry run - no GPT calls made, nothing written.")
        return

    # One call per (label, production batch), same as LabelSegments
    results: Dict[Tuple[str, str, int], Dict] = {}
    with ThreadPoolExecutor(max_workers=GPT_WORKERS) as executor:
        futures = {
            executor.submit(
                process_label_batch, label_defs[label],
                video_segments[video_id][start:start + BATCH_SIZE],
                f"{video_id}.json", start // BATCH_SIZE, args.model,
            ): (label, video_id, start)
            for label, video_id, start in tasks
        }
        for n, future in enumerate(as_completed(futures), 1):
            decisions = future.result() or {}
            results[futures[future]] = {str(seg_id): d for seg_id, d in decisions.items()}
            print(f"  {n}/{len(tasks)} calls done", end="\r")
    print()

    # Write the new workbook: original columns plus the new model's decision
    out_wb = openpyxl.Workbook()
    out = out_wb.active
    out.title = src.title
    headers = ["Segment Text", "Label", "4o-mini Reasoning", f"{args.model} Applies?",
               f"{args.model} Reasoning", "Agree?", "Comments", "Video ID", "Segment ID"]
    widths = [70, 22, 60, 16, 60, 18, 42, 24, 12]
    for col, (name, width) in enumerate(zip(headers, widths), 1):
        cell = out.cell(row=1, column=col, value=name)
        cell.font, cell.fill, cell.alignment = copy(src["A1"].font), copy(src["A1"].fill), copy(src["A1"].alignment)
        out.column_dimensions[openpyxl.utils.get_column_letter(col)].width = width

    failed = 0
    for i, row in enumerate(rows):
        text, label, reasoning, agree, comments = (row + [None] * 5)[:5]
        applies, new_reasoning = None, None
        if i in problems:
            new_reasoning = f"NOT RUN: {problems[i]}"
        else:
            decision = (results.get(row_batch[i]) or {}).get(row_key[i][1])
            if decision is None:
                failed += 1
                new_reasoning = "NOT RUN: GPT call failed after retries"
            else:
                applies, new_reasoning = decision["applied"], decision["rationale"]
        video_id, segment_id = row_key.get(i, (None, None))
        values = [text, label, reasoning, applies, new_reasoning, agree, comments, video_id, segment_id]
        for col, value in enumerate(values, 1):
            cell = out.cell(row=i + 2, column=col, value=value)
            cell.alignment = copy(src["A2"].alignment)

    out_wb.save(args.output)
    agreed = sum(1 for i in row_batch if (results.get(row_batch[i]) or {}).get(row_key[i][1], {}).get("applied"))
    print(f"{args.model} said the label applies on {agreed} of {len(row_batch)} matched rows; {failed} rows failed.")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
