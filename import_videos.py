"""
import_videos.py - Pipeline Orchestrator for Video Ingestion

This script orchestrates the end-to-end video processing pipeline:
1. Reads video manifest (videos.jsonl) generated from Box
2. Submits transcription jobs via TranscribeHttp function
3. Polls transcription status until completion
4. Triggers embedding and indexing via EmbedAndIndex function
5. Maintains progress state in pipeline_state.json for resumability

Architecture Role:
- Main entry point for batch video ingestion
- Coordinates between Azure Functions (TranscribeHttp, EmbedAndIndex)
- Handles job polling, error recovery, and state persistence
- Can be resumed if interrupted (reads/writes pipeline_state.json)

Usage:
  python import_videos.py

Configuration (via .env):
  - TRANSCRIBE_URL: TranscribeHttp function endpoint
  - EMBED_INDEX_URL: EmbedAndIndex function endpoint
  - POLL_SECONDS: Polling interval (default: 15)
  - MAX_ACTIVE: Max concurrent jobs to poll (default: 10)
"""

from dotenv import load_dotenv
import json
import os
import time
import requests


load_dotenv()

TRANSCRIBE_URL = os.environ["TRANSCRIBE_URL"]          # .../api/TranscribeHttp?code=...
EMBED_INDEX_URL = os.environ["EMBED_INDEX_URL"]        # .../api/EmbedAndIndex?code=...
SEGMENTS_CONTAINER = os.environ.get("SEGMENTS_CONTAINER", "segments")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "15"))
MAX_ACTIVE = int(os.environ.get("MAX_ACTIVE", "10"))   # how many jobs to poll at once

def post(url, payload, timeout=60):
    r = requests.post(url, json=payload, headers={"Content-Type":"application/json"}, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} calling {url}\nResponse: {r.text}")
    return r.json() if r.text else {}


def load_manifest(path="videos.jsonl"):
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items

def main():
    manifest = load_manifest("videos.jsonl")

    # progress file so you can resume
    state_path = "pipeline_state.json"
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}  # video_id -> {job_url, status, ...}

    # 1) submit jobs for anything not submitted
    for item in manifest:
        vid = item["video_id"]
        if state.get(vid, {}).get("status") in ("submitted", "running", "succeeded", "indexed", "failed"):
            continue

        print(f"Submitting {vid} ...")
        resp = post(TRANSCRIBE_URL, {
            "media_url": item["media_url"],
            "video_id": vid,
            "locale": item.get("locale", "en-US"),
            "auto_segment": True,
            "segment_ms": 30000,
        })
        state[vid] = {"job_url": resp["job_url"], "status": "submitted", "media_url": item["media_url"]}
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

    # 2) poll + index
    remaining = [vid for vid, s in state.items() if s.get("status") in ("submitted", "running")]
    while remaining:
        batch = remaining[:MAX_ACTIVE]
        for vid in batch:
            job_url = state[vid]["job_url"]
            print(f"Polling {vid} ...")
            resp = post(TRANSCRIBE_URL, {"job_url": job_url}, timeout=60)
            status = resp.get("status")

            if status in ("NotStarted", "Running"):
                state[vid]["status"] = "running"
            elif status == "Failed":
                state[vid]["status"] = "failed"
                state[vid]["error"] = resp
            elif status == "Succeeded":
                state[vid]["status"] = "succeeded"
                # ✅ use the authoritative path from TranscribeHttp
                segments_blob = resp.get("segments_blob")
                if not segments_blob:
                    # fallback if somehow missing
                    segments_blob = f"{SEGMENTS_CONTAINER}/{vid}.json"
                state[vid]["segments_blob"] = segments_blob
                print(f"Indexing {vid} from {segments_blob} ...")
                idx = post(EMBED_INDEX_URL, {
                    "segments_blob": segments_blob,
                    "source_url": state[vid].get("media_url", ""),
                    "source_type": "box",
                }, timeout=180)
                state[vid]["status"] = "indexed"
                state[vid]["index_result"] = idx
            else:
                state[vid]["status"] = f"unknown:{status}"
                state[vid]["last_response"] = resp

            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)

        # refresh remaining
        remaining = [vid for vid, s in state.items() if s.get("status") in ("submitted", "running")]
        time.sleep(POLL_SECONDS)

    print("✅ All done. See pipeline_state.json")

if __name__ == "__main__":
    main()
