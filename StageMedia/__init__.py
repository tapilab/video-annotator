"""
StageMedia - Azure Function for staging incoming media into storage

Takes a video from one of three sources - a YouTube link, a Box link, or a
file already in hand - and produces a link in our own storage account that
the transcription backend (TranscribeHttp) can read.

A source that is already a plain, reachable URL needs no staging at all -
it goes straight to TranscribeHttp and never calls this function.

Input:
  YouTube/Box (JSON body):
    { "source_type": "youtube" | "box", "url": "...", "video_id": "..." }
  File upload (query string + raw body):
    POST ?source_type=upload&video_id=...&filename=...
    body: raw file bytes

Output: JSON with { "media_url": "<signed blob URL>", "video_id": ..., "source_type": ... }
"""

import json
import logging
import os
import re
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse, parse_qs

import azure.functions as func
import requests
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions

INPUT_CONTAINER = os.environ.get("INPUT_CONTAINER", "speech-input")


def _download_youtube_audio(youtube_url: str, output_path: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Uses yt-dlp as a Python library rather than shelling out to it as a command -
    Azure's environment doesn't expose installed packages to a freshly spawned
    subprocess the way it does to the function's own already-running code, so
    the subprocess approach that works locally fails here.

    Only accepts YouTube's native m4a audio stream, with no conversion step.
    That avoids needing ffmpeg entirely, which Azure's environment doesn't have.
    A video with no m4a audio stream available is rejected with a clear error,
    rather than guessing whether some other format would transcribe correctly.
    """
    if not youtube_url or not youtube_url.strip():
        return None, "YouTube URL is empty"
    try:
        import yt_dlp
    except ImportError as e:
        return None, f"yt-dlp not available ({type(e).__name__}: {e})"

    base = output_path.rsplit(".", 1)[0]
    ydl_opts = {
        "format": "bestaudio[ext=m4a]",
        "outtmpl": f"{base}.%(ext)s",
        "nocheckcertificate": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # YouTube's bot-check can reject the default client; trying a couple
        # of alternates in order improves the odds one of them is let through.
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            },
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url.strip()])
    except Exception as e:
        return None, f"yt-dlp failed: {type(e).__name__}: {e}"

    final_path = f"{base}.m4a"
    if os.path.exists(final_path):
        return final_path, None
    return None, "Download completed but no m4a audio stream was found for this video"


def _download_box_audio(box_url: str, output_path: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Download audio from a Box shared file URL.

    Handles three URL patterns:
      - /s/{token}              shared links  -> append ?dl=1
      - /file/{id}?s={token}   viewer links  -> /content or index.php endpoints
      - /shared/static/{hash}  direct links  -> use as-is
    """
    if not box_url or not box_url.strip():
        return None, "Box URL is empty"

    try:
        parsed = urlparse(box_url.strip())
        qs = parse_qs(parsed.query)
        base = f"{parsed.scheme}://{parsed.netloc}"
        url_lower = box_url.lower()

        file_id_match = re.search(r"/file/(\d+)", parsed.path)
        shared_token = qs.get("s", [None])[0]
        s_path_match = re.match(r"/s/([^/?#]+)", parsed.path)

        candidates = []

        if s_path_match:
            s_token = s_path_match.group(1)
            candidates.append(f"{base}/s/{s_token}?dl=1")
            candidates.append(f"{base}/shared/static/{s_token}")
        elif file_id_match and shared_token:
            file_id = file_id_match.group(1)
            candidates.append(f"{base}/file/{file_id}/content?s={shared_token}")
            candidates.append(
                f"{base}/index.php"
                f"?rm=box_download_shared_file"
                f"&file_id=f_{file_id}"
                f"&shared_name={shared_token}"
            )
        elif "/shared/static/" in url_lower:
            candidates.append(box_url.strip())
        else:
            candidates.append(box_url.strip())

        req_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }

        last_error = "All download attempts failed"

        for attempt_url in candidates:
            try:
                with requests.get(
                    attempt_url, headers=req_headers,
                    stream=True, timeout=300, allow_redirects=True
                ) as r:
                    r.raise_for_status()
                    content_type = r.headers.get("Content-Type", "")
                    if "text/html" in content_type:
                        last_error = f"URL returned HTML (not audio): {attempt_url}"
                        continue

                    ext = ".m4a"
                    ct_lower = content_type.lower()
                    if "mp3" in ct_lower or "mpeg" in ct_lower:
                        ext = ".mp3"
                    elif "wav" in ct_lower:
                        ext = ".wav"
                    elif "mp4" in ct_lower:
                        ext = ".mp4"
                    elif "ogg" in ct_lower:
                        ext = ".ogg"

                    disposition = r.headers.get("Content-Disposition", "")
                    cd_match = re.search(r"filename=[\"']?([^\"';\s]+)", disposition)
                    if cd_match:
                        cd_name = cd_match.group(1)
                        for candidate_ext in [".m4a", ".mp3", ".wav", ".mp4", ".webm", ".ogg"]:
                            if cd_name.lower().endswith(candidate_ext):
                                ext = candidate_ext
                                break

                    url_path = attempt_url.split("?")[0]
                    for candidate_ext in [".m4a", ".mp3", ".wav", ".mp4", ".webm", ".ogg"]:
                        if url_path.lower().endswith(candidate_ext):
                            ext = candidate_ext
                            break

                    final_path = (
                        output_path if output_path.lower().endswith(ext)
                        else output_path.rsplit(".", 1)[0] + ext
                    )

                    with open(final_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)

                    if os.path.exists(final_path) and os.path.getsize(final_path) > 0:
                        return final_path, None

                    last_error = "Downloaded file is empty or missing"

            except Exception as e:
                last_error = str(e)
                continue

        return None, (
            f"Could not download the Box file automatically ({last_error}). "
            "Try opening the Box link, clicking Download, and using the resulting "
            "shared/static/... URL instead."
        )

    except Exception as e:
        return None, f"Box download error: {str(e)}"


def _upload_to_blob(file_bytes: bytes, blob_name: str) -> str:
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    key = os.environ["AZURE_STORAGE_KEY"]
    service = BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=key,
    )
    try:
        service.get_container_client(INPUT_CONTAINER).create_container()
    except Exception:
        pass
    blob_client = service.get_blob_client(container=INPUT_CONTAINER, blob=blob_name)
    blob_client.upload_blob(file_bytes, overwrite=True)

    sas_token = generate_blob_sas(
        account_name=account,
        container_name=INPUT_CONTAINER,
        blob_name=blob_name,
        account_key=key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=24),
        protocol="https",
    )
    return f"https://{account}.blob.core.windows.net/{INPUT_CONTAINER}/{blob_name}?{sas_token}"


def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        source_type = req.params.get("source_type")
        video_id = req.params.get("video_id")
        body = {}

        if not source_type:
            try:
                body = req.get_json()
            except ValueError:
                body = {}
            source_type = body.get("source_type")
            video_id = video_id or body.get("video_id")

        if not source_type or not video_id:
            return func.HttpResponse(
                json.dumps({"error": "'source_type' and 'video_id' are required"}),
                mimetype="application/json",
                status_code=400,
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            if source_type == "youtube":
                url = body.get("url")
                if not url:
                    return func.HttpResponse(
                        json.dumps({"error": "'url' is required for source_type 'youtube'"}),
                        mimetype="application/json", status_code=400,
                    )
                output_path = f"{tmpdir}/{video_id}.m4a"
                downloaded_path, error = _download_youtube_audio(url, output_path)
                if error:
                    return func.HttpResponse(
                        json.dumps({"error": f"YouTube download failed: {error}"}),
                        mimetype="application/json", status_code=502,
                    )
                with open(downloaded_path, "rb") as f:
                    file_bytes = f.read()
                blob_name = f"youtube_{video_id}_{int(time.time())}{Path(downloaded_path).suffix}"

            elif source_type == "box":
                url = body.get("url")
                if not url:
                    return func.HttpResponse(
                        json.dumps({"error": "'url' is required for source_type 'box'"}),
                        mimetype="application/json", status_code=400,
                    )
                output_path = f"{tmpdir}/{video_id}.m4a"
                downloaded_path, error = _download_box_audio(url, output_path)
                if error:
                    return func.HttpResponse(
                        json.dumps({"error": f"Box download failed: {error}"}),
                        mimetype="application/json", status_code=502,
                    )
                with open(downloaded_path, "rb") as f:
                    file_bytes = f.read()
                blob_name = f"box_{video_id}_{int(time.time())}{Path(downloaded_path).suffix}"

            elif source_type == "upload":
                file_bytes = req.get_body()
                if not file_bytes:
                    return func.HttpResponse(
                        json.dumps({"error": "No file bytes received"}),
                        mimetype="application/json", status_code=400,
                    )
                filename = req.params.get("filename", "upload.m4a")
                suffix = Path(filename).suffix or ".m4a"
                blob_name = f"upload_{video_id}_{int(time.time())}{suffix}"

            else:
                return func.HttpResponse(
                    json.dumps({"error": "source_type must be one of: youtube, box, upload"}),
                    mimetype="application/json", status_code=400,
                )

            media_url = _upload_to_blob(file_bytes, blob_name)

        return func.HttpResponse(
            json.dumps({"media_url": media_url, "video_id": video_id, "source_type": source_type}),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logging.exception("StageMedia failed")
        return func.HttpResponse(
            json.dumps({"error": str(e), "trace": traceback.format_exc()}),
            mimetype="application/json",
            status_code=500,
        )
