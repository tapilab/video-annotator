"""
scripts/compare_label_samples.py - Re-judge a label sample review workbook with another model and score agreement with the experts

Settings come from local.settings.json.

Usage:
  python scripts/compare_label_samples.py IN.xlsx OUT.xlsx --dry-run   # check labels only, no GPT calls
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
from azure.storage.blob import BlobServiceClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from shared.gpt_labeling import BATCH_SIZE, GPT_WORKERS, process_label_batch


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-judge a label sample review workbook with another model.")
    parser.add_argument("input", help="review workbook (Segment Text, Label, Reasoning, Agree?, Comments)")
    parser.add_argument("output", help="workbook to write")
    parser.add_argument("--model", default="gpt-5-mini", help="model name sent to the proxy (default: gpt-5-mini)")
    parser.add_argument("--dry-run", action="store_true", help="check labels and count calls only, no GPT calls")
    args = parser.parse_args()

    _load_settings()
    src_wb = openpyxl.load_workbook(args.input)
    src = src_wb.active
    rows = [list(r) for r in src.iter_rows(min_row=2, values_only=True) if r[0]]

    label_defs = _read_label_defs()
    print(f"Rows: {len(rows)}")

    # Batch the workbook's own rows by label, so only these segments are judged
    problems: Dict[int, str] = {}
    rows_by_label: Dict[str, List[int]] = defaultdict(list)
    for i, (text, label, *_rest) in enumerate(rows):
        if label not in label_defs:
            problems[i] = f"label '{label}' not active in the label library"
        else:
            rows_by_label[label].append(i)

    tasks: Dict[Tuple[str, int], List[Dict]] = {}  # (label, batch number) -> segments
    row_batch: Dict[int, Tuple[str, int]] = {}
    for label, idxs in rows_by_label.items():
        for start in range(0, len(idxs), BATCH_SIZE):
            chunk = idxs[start:start + BATCH_SIZE]
            tasks[(label, start // BATCH_SIZE)] = [{"segment_id": str(i), "text": rows[i][0]} for i in chunk]
            for i in chunk:
                row_batch[i] = (label, start // BATCH_SIZE)

    print(f"Rows to judge: {len(row_batch)}   Problems: {len(problems)}   GPT calls needed: {len(tasks)}")
    for i, msg in sorted(problems.items()):
        print(f"  row {i + 2}: {msg}")
    if args.dry_run:
        print("\nDry run - no GPT calls made, nothing written.")
        return

    # One call per (label, batch of workbook rows)
    results: Dict[Tuple[str, int], Dict] = {}
    with ThreadPoolExecutor(max_workers=GPT_WORKERS) as executor:
        futures = {
            executor.submit(
                process_label_batch, label_defs[label], segments,
                os.path.basename(args.input), batch_no, args.model,
            ): (label, batch_no)
            for (label, batch_no), segments in tasks.items()
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
               f"{args.model} Reasoning", "Agree?", "Comments"]
    widths = [70, 22, 60, 16, 60, 18, 42]
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
            decision = (results.get(row_batch[i]) or {}).get(str(i))
            if decision is None:
                failed += 1
                new_reasoning = "NOT RUN: GPT call failed after retries"
            else:
                applies, new_reasoning = decision["applied"], decision["rationale"]
        values = [text, label, reasoning, applies, new_reasoning, agree, comments]
        for col, value in enumerate(values, 1):
            cell = out.cell(row=i + 2, column=col, value=value)
            cell.alignment = copy(src["A2"].alignment)

    out_wb.save(args.output)
    # Agreement with the experts: model says applies and expert agreed, or says not and expert disagreed
    counts = defaultdict(int)
    for i in row_batch:
        decision = (results.get(row_batch[i]) or {}).get(str(i))
        if decision is not None:
            counts[(bool(decision["applied"]), rows[i][3] is True)] += 1
    judged = sum(counts.values())
    matches = counts[(True, True)] + counts[(False, False)]
    print(f"{args.model} judged {judged} of {len(row_batch)} rows; {failed} rows failed.")
    if judged:
        print(f"Agreement with experts: {matches}/{judged} = {matches / judged:.1%}")
        print(f"  applies, expert agreed:          {counts[(True, True)]}")
        print(f"  applies, expert disagreed:       {counts[(True, False)]}")
        print(f"  doesn't apply, expert agreed:    {counts[(False, True)]}")
        print(f"  doesn't apply, expert disagreed: {counts[(False, False)]}")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
