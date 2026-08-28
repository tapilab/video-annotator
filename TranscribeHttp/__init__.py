"""
TranscribeHttp - Azure Function for Batch Transcription

This Azure Function handles the transcription workflow for video files:
1. Submits batch transcription jobs to Azure Speech Service
2. Polls job status until completion
3. Downloads and normalizes transcripts with word-level timestamps
4. Segments transcripts into 30-second clips
5. Writes segments to Azure Blob Storage

Architecture Role:
- Part of the ingestion pipeline (called by import_videos.py)
- First step in processing videos from Box
- Outputs segment JSON files to Blob Storage for downstream indexing

Input: POST with either:
  - media_url: Submit new transcription job
  - job_url: Check status or fetch completed transcript

Output: JSON with job_url (submission) or status/result (polling)
"""

import json
import logging
import os
import traceback
import azure.functions as func

from shared.speech_batch import (
    SpeechConfig,
    submit_transcription_job,
    get_job,
    get_transcript_urls,
    download_and_normalize,
)
from shared.segmenter import segment_utterances
from shared.util import write_json_blob

def _cfg() -> SpeechConfig:
    endpoint = os.environ.get("SPEECH_ENDPOINT")
    if not endpoint:
        region = os.environ.get("SPEECH_REGION", "eastus")
        endpoint = f"https://{region}.api.cognitive.microsoft.com"
    return SpeechConfig(
        key=os.environ["SPEECH_KEY"],
        endpoint=endpoint.rstrip("/"),
        api_version=os.environ.get("SPEECH_API_VERSION", "2024-11-15"),
    )

def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        cfg = _cfg()
        body = req.get_json()
        locale = body.get("locale", "en-US")

        # Mode 1: submit
        media_url = body.get("media_url")
        if media_url:
            job_url = submit_transcription_job(
                config=cfg,
                content_urls=[media_url],
                locale=locale,
                display_name=body.get("display_name") or "transcription",
                word_level_timestamps=True,
                ttl_hours=24,
            )
            return func.HttpResponse(
                json.dumps({"job_url": job_url}),
                mimetype="application/json",
                status_code=202,
            )

        # Mode 2: check status / fetch result
        job_url = body.get("job_url") or req.params.get("job_url")
        if job_url:
            job = get_job(cfg, job_url)
            status = job.get("status")

            if status in ("NotStarted", "Running"):
                return func.HttpResponse(
                    json.dumps({"status": status, "job_url": job_url}),
                    mimetype="application/json",
                    status_code=200,
                )

            if status == "Failed":
                return func.HttpResponse(
                    json.dumps({"status": status, "job": job}),
                    mimetype="application/json",
                    status_code=200,
                )

            if status == "Succeeded":
                urls = get_transcript_urls(cfg, job_url)
                result = download_and_normalize(urls, prefer_channel=0, dedupe=True)
                utterances = result.get("utterances") or []
                segment_ms = int(body.get("segment_ms", 30000))
                segments = segment_utterances(utterances, segment_ms=segment_ms)

                # Write segments to Blob
                segments_container = os.environ.get("SEGMENTS_CONTAINER", "segments")
                # Choose/derive a video_id (simple MVP: hash job_url)
                video_id = body.get("video_id") or "vid_" + str(abs(hash(job_url)))
                segments_payload = {
                    "video_id": video_id,
                    "segment_ms": segment_ms,
                    "num_segments": len(segments),
                    "segments": segments,
                }
                segments_blob = write_json_blob(segments_container, f"{video_id}.json", segments_payload)


                return func.HttpResponse(
                    json.dumps({"status": status, "result": result, "segments_blob": segments_blob}),
                    mimetype="application/json",
                    status_code=200,
                )

            return func.HttpResponse(
                json.dumps({"status": status, "job": job}),
                mimetype="application/json",
                status_code=200,
            )

        return func.HttpResponse(
            json.dumps({"error": "Send either 'media_url' to submit, or 'job_url' to check status."}),
            mimetype="application/json",
            status_code=400,
        )

    except Exception as e:
        logging.exception("TranscribeHttp failed")
        return func.HttpResponse(
            json.dumps({"error": str(e), "trace": traceback.format_exc()}),
            mimetype="application/json",
            status_code=500,
        )
