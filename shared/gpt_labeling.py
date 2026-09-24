"""
shared/gpt_labeling.py - The GPT labeling call (prompt, validation, retries) shared by LabelSegments and EvalLabels
"""

import logging
import os
import time
from typing import Dict, List, Optional

from openai import OpenAI
from pydantic import BaseModel

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", 10))
GPT_WORKERS = int(os.environ.get("GPT_WORKERS", 5))


class SegmentDecision(BaseModel):
    segment_id: str
    applied: bool
    rationale: str


class LabelJudgment(BaseModel):
    results: List[SegmentDecision]


def _format_label(label_def: Dict) -> str:
    description = label_def["description"]
    examples = label_def.get("examples") or []
    if examples:
        description += " " + " ".join(f'For example, the {ex}.' for ex in examples)
    return f"Name: {label_def['name']}\nDescription: {description}"


def _format_segments(seg_inputs: List[Dict]) -> str:
    return "\n".join(f"[{s['segment_id']}] {s['text']}" for s in seg_inputs)


def _call_gpt(label_def: Dict, seg_inputs: List[Dict]) -> List[SegmentDecision]:
    """Call GPT-4o-mini via proxy to judge one label against a batch of segments."""
    client = OpenAI(
        base_url=os.environ["PROXY_BASE_URL"],
        api_key="unused",
        default_headers={"x-functions-key": os.environ["FUNCTION_HOST_KEY"]},
    )

    system_prompt = (
        "You are a content labeler. You will be given one label, the label's definition, and a list of video transcript "
        "segments. For every segment, decide whether the label applies. Mark true if the segment "
        "directly mentions, discusses, or is obviously related to the label topic. Mark false if the label does not "
        "mention, discuss, or if it is not related. Give a brief rationale either way. "
        "Judge every segment listed, including ones where the label clearly does not apply."
    )

    user_prompt = (
        f"LABEL\n{_format_label(label_def)}\n\n"
        f"SEGMENTS\n{_format_segments(seg_inputs)}"
    )

    resp = client.responses.parse(
        model="gpt-4o-mini",
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        text_format=LabelJudgment,
    )

    return resp.output_parsed.results


def process_label_batch(
    label_def: Dict,
    batch: List[Dict],
    blob_name: str,
    batch_idx: int,
) -> Optional[Dict[str, Dict]]:
    """Judge one label against one batch of segments, retrying with backoff.

    Returns {segment_id: {"applied": bool, "rationale": str}} covering every
    segment in the batch, or None if it still fails (errors, or the model
    skipping segments) after all attempts.
    """
    seg_inputs = [{"segment_id": s["segment_id"], "text": s["text"]} for s in batch]
    expected_ids = {s["segment_id"] for s in batch}

    for attempt in range(4):
        try:
            decisions = _call_gpt(label_def, seg_inputs)
            result = {d.segment_id: {"applied": d.applied, "rationale": d.rationale} for d in decisions}
            missing = expected_ids - result.keys()
            if missing:
                raise ValueError(f"model skipped {len(missing)} of {len(expected_ids)} segments")
            return result
        except Exception as e:
            if attempt == 3:
                logging.warning(
                    f"GPT call failed for label '{label_def.get('name')}' on {blob_name} "
                    f"batch {batch_idx}: {e}"
                )
            else:
                time.sleep(2 * 2 ** attempt)

    return None
