"""
EvalLabels - Azure Function for evaluating labeling accuracy

Accepts a list of test cases (text + expected labels), runs them through
the same GPT labeler used by LabelSegments, and returns per-row results
and per-label accuracy metrics. The judging logic lives in
shared/gpt_labeling.py and is used by both functions, so eval always matches
production labeling: one label at a time against small batches of rows, with
each label's positive examples, retries with backoff, and failed labels
reported instead of silently scored as "predicted nothing".

Input: POST with:
  {
    "test_cases": [
      {"text": "...", "expected_labels": ["Label1", "Label2"]},
      ...
    ]
  }

Output: JSON with:
  - rows: per-row comparison (expected, predicted, correct, missed, hallucinated)
  - metrics: per-label precision/recall/F1 + macro/micro F1
  - unknown_labels: expected labels not found in the label library (ignored)
  - failed_labels: labels whose AI calls still failed after retries; their
    scores are unreliable

Environment Variables:
  AZURE_STORAGE_ACCOUNT   - Storage account name
  AZURE_STORAGE_KEY       - Storage account key
  LABELS_CONTAINER        - Blob container for label library (default: "labels")
  PROXY_BASE_URL          - Azure OpenAI proxy base URL
  FUNCTION_HOST_KEY       - Azure Function host key for the proxy
  BATCH_SIZE              - Rows per GPT call, per label (default: 10)
  GPT_WORKERS             - Parallel GPT calls (default: 5)
"""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Set

import azure.functions as func
from azure.storage.blob import BlobServiceClient

from shared.gpt_labeling import BATCH_SIZE, GPT_WORKERS, process_label_batch

def _read_label_json() -> Dict[str, Any]:
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    key = os.environ["AZURE_STORAGE_KEY"]
    service = BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=key,
    )
    container = os.environ.get("LABELS_CONTAINER", "labels")
    bc = service.get_blob_client(container=container, blob="label_library.json")
    return json.loads(bc.download_blob().readall())


def _compute_metrics(expected_list: List[List[str]], predictions: List[List[str]]) -> Dict:
    # Collect all label names that appear in annotations
    all_labels = set()
    for expected in expected_list:
        all_labels.update(expected)

    per_label = {}
    for label in sorted(all_labels):
        tp = sum(
            1 for expected, pred in zip(expected_list, predictions)
            if label in expected and label in pred
        )
        fp = sum(
            1 for expected, pred in zip(expected_list, predictions)
            if label not in expected and label in pred
        )
        fn = sum(
            1 for expected, pred in zip(expected_list, predictions)
            if label in expected and label not in pred
        )
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        per_label[label] = {
            "tp": tp, "fp": fp, "fn": fn,
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
        }
    
    
    macro_f1 = sum(m["f1"] for m in per_label.values()) / len(per_label) if per_label else 0.0
    total_tp = sum(m["tp"] for m in per_label.values())
    total_fp = sum(m["fp"] for m in per_label.values())
    total_fn = sum(m["fn"] for m in per_label.values())
    micro_p = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 0.0
    micro_r = total_tp / (total_tp + total_fn) if (total_tp + total_fn) > 0 else 0.0
    micro_f1 = 2 * micro_p * micro_r / (micro_p + micro_r) if (micro_p + micro_r) > 0 else 0.0

    return {
        "per_label": per_label,
        "macro_f1": round(macro_f1, 3),
        "micro_f1": round(micro_f1, 3),
        "micro_precision": round(micro_p, 3),
        "micro_recall": round(micro_r, 3),
    }


def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
        test_cases = body.get("test_cases", [])

        if not test_cases:
            return func.HttpResponse(
                json.dumps({"error": "No test cases provided."}),
                mimetype="application/json",
                status_code=400,
            )

        library = _read_label_json()
        active_labels = [l for l in library.get("labels", []) if l.get("is_active", True)]
        label_defs = [{"name": l["name"], "description": l["description"], "examples": l.get("examples", [])} for l in active_labels]
        valid_names = {l["name"] for l in active_labels}

        if not label_defs:
            return func.HttpResponse(
                json.dumps({"error": "No active labels found."}),
                mimetype="application/json",
                status_code=400,
            )

        # Judge one label at a time against small batches of rows, exactly as LabelSegments
        # does. Each row's global index is its segment_id; rows with no text are skipped.
        segments = [
            {"segment_id": str(idx), "text": tc["text"]}
            for idx, tc in enumerate(test_cases)
            if (tc.get("text") or "").strip()
        ]
        tasks = [
            (label_def, segments[i:i + BATCH_SIZE], i)
            for label_def in label_defs
            for i in range(0, len(segments), BATCH_SIZE)
        ]

        results_by_segment: Dict[str, Dict[str, Dict]] = {}
        failed_labels: Set[str] = set()
        with ThreadPoolExecutor(max_workers=GPT_WORKERS) as executor:
            futures = {
                executor.submit(process_label_batch, label_def, batch, "eval", batch_idx): label_def["name"]
                for label_def, batch, batch_idx in tasks
            }
            for future in as_completed(futures):
                label_name = futures[future]
                try:
                    decisions = future.result()
                except Exception as e:
                    logging.warning(f"Label batch failed for '{label_name}' in eval: {e}")
                    decisions = None
                if decisions is None:
                    failed_labels.add(label_name)
                    continue
                for seg_id, decision in decisions.items():
                    results_by_segment.setdefault(seg_id, {})[label_name] = decision

        # Build per-row results
        predictions: List[List[str]] = []
        rows = []
        unknown_labels: set = set()
        for idx, tc in enumerate(test_cases):
            seg_decisions = results_by_segment.get(str(idx), {})
            validated = [
                {"name": d["name"], "applied": seg_decisions[d["name"]]["applied"],
                 "rationale": seg_decisions[d["name"]]["rationale"]}
                for d in label_defs if d["name"] in seg_decisions
            ]
            predicted = [l["name"] for l in validated if l["applied"]]
            raw_expected = tc.get("expected_labels", [])
            row_unknown = [l for l in raw_expected if l not in valid_names]
            unknown_labels.update(row_unknown)
            expected = [l for l in raw_expected if l in valid_names]

            predictions.append(predicted)
            rows.append({
                "text": tc["text"],
                "expected": expected,
                "predicted": predicted,
                "correct": [l for l in predicted if l in expected],
                "missed": [l for l in expected if l not in predicted],
                "hallucinated": [l for l in predicted if l not in expected],
                "details": validated,
            })

        filtered_expected = [row["expected"] for row in rows]
        metrics = _compute_metrics(filtered_expected, predictions)

        return func.HttpResponse(
            json.dumps({"rows": rows, "metrics": metrics, "unknown_labels": list(unknown_labels), "failed_labels": sorted(failed_labels)}, ensure_ascii=False),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logging.exception("EvalLabels failed")
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            mimetype="application/json",
            status_code=500,
        )
