"""
1_Upload.py - Upload & Submit page for VANTAGE-AI

Submits a video for processing and returns immediately - it does not wait
for transcription to finish. Progress is checked later on the Manage Videos
page, not watched live here.
"""

import sys
sys.path.append("..")
import streamlit as st
import pandas as pd
import io
import os
import re
from datetime import datetime, timezone
import requests
from utils import (
    AZURE_STORAGE_KEY,
    generate_video_id,
    detect_url_type,
    get_pending_uploads,
    save_pending_uploads,
)

STAGE_MEDIA_URL = os.environ.get("STAGE_MEDIA_URL", "")
TRANSCRIBE_URL = os.environ.get("TRANSCRIBE_URL", "")

APP_TITLE = "VANTAGE-AI: Video ANnotation, TAGging & Exploration"
st.title(APP_TITLE)
st.subheader("Upload Video for Transcription")

azure_configured = bool(AZURE_STORAGE_KEY) and bool(STAGE_MEDIA_URL) and bool(TRANSCRIBE_URL)
if not azure_configured:
    st.error("⚠️ STAGE_MEDIA_URL, TRANSCRIBE_URL, and Azure Storage must be configured. Check .env file.")


# ---------------------------------------------------------------------------
# Backend calls
# ---------------------------------------------------------------------------
def stage_media(source_type: str, video_id: str, url: str = None,
                 file_bytes: bytes = None, filename: str = None):
    """Get the media into storage and back a URL the transcription backend can read."""
    try:
        if source_type in ("youtube", "box"):
            r = requests.post(
                STAGE_MEDIA_URL,
                json={"source_type": source_type, "url": url, "video_id": video_id},
                timeout=600,
            )
        elif source_type == "upload":
            r = requests.post(
                STAGE_MEDIA_URL,
                params={"source_type": "upload", "video_id": video_id, "filename": filename or "upload.m4a"},
                data=file_bytes,
                timeout=600,
            )
        else:
            return None, f"Unknown source_type for staging: {source_type}"

        if r.status_code >= 400:
            return None, (r.json().get("error", r.text) if r.text else f"HTTP {r.status_code}")
        return r.json().get("media_url"), None
    except requests.exceptions.RequestException as e:
        return None, str(e)


def submit_transcription(media_url: str, video_id: str):
    """Submit a transcription job. Returns (job_url, error)."""
    try:
        r = requests.post(
            TRANSCRIBE_URL,
            json={"media_url": media_url, "video_id": video_id, "locale": "en-US"},
            timeout=60,
        )
        if r.status_code >= 400:
            return None, (r.json().get("error", r.text) if r.text else f"HTTP {r.status_code}")
        return r.json().get("job_url"), None
    except requests.exceptions.RequestException as e:
        return None, str(e)


def submit_video(source_type: str, source_url: str, video_id: str,
                  file_bytes: bytes = None, filename: str = None):
    """
    Stage (if needed) and submit one video. source_url is the original,
    user-facing link (or 'uploaded_file://<id>' for File Upload) - it's
    what gets remembered as where the video came from, never an internal
    storage link. Returns (video_id, error).
    """
    if source_type == "direct":
        media_url = source_url
    else:
        media_url, error = stage_media(source_type, video_id, url=source_url,
                                        file_bytes=file_bytes, filename=filename)
        if error:
            return video_id, f"Staging failed: {error}"

    job_url, error = submit_transcription(media_url, video_id)
    if error:
        return video_id, f"Transcription submit failed: {error}"

    pending = get_pending_uploads()
    pending[video_id] = {
        "job_url": job_url,
        "source_url": source_url,
        "source_type": source_type,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    save_pending_uploads(pending)
    return video_id, None


# ---------------------------------------------------------------------------
# Source selection
# ---------------------------------------------------------------------------
source_type = st.radio(
    "Select Source",
    ["File Upload", "Direct URL", "YouTube", "📁 Batch CSV Upload"],
    horizontal=True,
)

media_url = None
video_id = None
file_bytes = None
yt_url = None
csv_df = None
detected_source_type = "unknown"
uploaded_filename = None

# ---------------------------------------------------------------------------
# File Upload
# ---------------------------------------------------------------------------
if source_type == "File Upload":
    if not azure_configured:
        st.info("Please configure Azure Storage and the backend URLs to enable file upload")
    else:
        uploaded_file = st.file_uploader(
            "Choose video/audio file",
            type=["mp4", "avi", "mov", "mkv", "m4a", "mp3", "wav"],
            accept_multiple_files=False,
        )
        if uploaded_file:
            st.success(f"📁 {uploaded_file.name} ({uploaded_file.size / 1024 / 1024:.1f} MB)")
            file_bytes = uploaded_file.getvalue()
            uploaded_filename = uploaded_file.name
            video_id = generate_video_id(uploaded_file.name)
            detected_source_type = "upload"
            st.info("File ready to submit")

# ---------------------------------------------------------------------------
# Direct URL  (includes Box URLs)
# ---------------------------------------------------------------------------
elif source_type == "Direct URL":
    url_input = st.text_input(
        "Media URL",
        placeholder=(
            "https://tulane.app.box.com/file/... "
            "or https://example.com/audio.mp3"
        ),
    )
    if url_input.strip():
        media_url = url_input.strip()
        video_id = generate_video_id(url_input)
        url_type_detected = detect_url_type(url_input.strip())
        detected_source_type = url_type_detected  # "box", "direct", etc.
        if url_type_detected == "box":
            if "/file/" in url_input:
                st.warning(
                    "📦 **Box viewer link detected.** "
                    "Attempting automatic download — this works for publicly shared files. "
                    "If it fails, click the **Download (↓)** button on the Box page "
                    "and paste the resulting `shared/static/...` URL here instead."
                )
            else:
                st.info(
                    "📦 Box URL detected — file will be downloaded and "
                    "re-uploaded to Azure for transcription."
                )
        else:
            st.success("✅ URL validated")

# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------
elif source_type == "YouTube":
    yt_url = st.text_input(
        "YouTube URL",
        placeholder="https://youtube.com/watch?v=...",
    )
    if yt_url and yt_url.strip():
        video_id = generate_video_id(f"yt_{yt_url.strip()}")
        detected_source_type = "youtube"
        st.success("YouTube URL ready")

# ---------------------------------------------------------------------------
# Batch CSV Upload
# ---------------------------------------------------------------------------
elif source_type == "📁 Batch CSV Upload":
    st.subheader("📁 Batch Process Videos from CSV")

    csv_file = st.file_uploader(
        "Upload CSV file",
        type=["csv"],
        help="CSV must contain a column with video URLs",
    )

    if csv_file:
        try:
            try:
                csv_df = pd.read_csv(csv_file)
            except Exception:
                csv_file.seek(0)
                csv_df = pd.read_csv(csv_file, header=None)
                csv_df.columns = [f"column_{i}" for i in range(len(csv_df.columns))]

            # Handle CSVs where the header row itself is a URL
            url_like_columns = [
                col for col in csv_df.columns
                if detect_url_type(str(col).strip()) != "unknown"
            ]
            if url_like_columns and len(csv_df.columns) == 1:
                url_col_name = csv_df.columns[0]
                new_row = {url_col_name: url_col_name}
                csv_df = pd.concat(
                    [pd.DataFrame([new_row]), csv_df], ignore_index=True
                )

            st.success(f"✅ Loaded CSV with {len(csv_df)} rows")

            url_column = st.selectbox(
                "Select column containing video URLs",
                options=csv_df.columns.tolist(),
            )
            id_column_options = ["Auto-generate"] + [
                c for c in csv_df.columns if c != url_column
            ]
            id_column = st.selectbox(
                "Select column for custom Video ID (optional)",
                options=id_column_options,
                index=0,
            )

            urls_raw = csv_df[url_column].dropna().astype(str).tolist()
            urls_to_process = [u.strip() for u in urls_raw if u.strip()]

            with st.expander(f"Preview URLs ({len(urls_to_process)} found)"):
                for i, url in enumerate(urls_to_process[:10], 1):
                    url_type = detect_url_type(url)
                    icon = (
                        "🎬" if url_type == "youtube"
                        else "📦" if url_type == "box"
                        else "📄" if url_type == "direct"
                        else "❓"
                    )
                    st.text(f"{i}. {icon} {url[:80]}...")

            valid_urls, invalid_urls = [], []
            for url in urls_to_process:
                if detect_url_type(str(url)) in ("youtube", "direct", "box"):
                    valid_urls.append(url)
                else:
                    invalid_urls.append(url)

            col1, col2, col3 = st.columns(3)
            col1.metric("Total", len(urls_to_process))
            col2.metric("✅ Valid", len(valid_urls))
            col3.metric("❌ Invalid", len(invalid_urls))

            st.session_state["batch_urls"] = valid_urls
            st.session_state["batch_df"] = csv_df
            st.session_state["batch_url_column"] = url_column
            st.session_state["batch_id_column"] = id_column

        except Exception as e:
            st.error(f"Error reading CSV: {e}")

# ---------------------------------------------------------------------------
# Custom Video ID (single-video modes only)
# ---------------------------------------------------------------------------
custom_id = st.text_input("Custom Video ID (optional)")
if custom_id.strip() and source_type != "📁 Batch CSV Upload":
    video_id = custom_id.strip()

# ---------------------------------------------------------------------------
# Enable / disable the submit button
# ---------------------------------------------------------------------------
can_process = False
if source_type == "File Upload":
    can_process = file_bytes is not None and azure_configured
elif source_type == "Direct URL":
    can_process = bool(media_url) and azure_configured
elif source_type == "YouTube":
    can_process = bool(yt_url and yt_url.strip()) and azure_configured
elif source_type == "📁 Batch CSV Upload":
    can_process = bool(st.session_state.get("batch_urls")) and azure_configured

button_text = "🚀 Submit for Transcription"
if source_type == "📁 Batch CSV Upload":
    count = len(st.session_state.get("batch_urls", []))
    button_text = f"🚀 Submit {count} Videos for Transcription"

# ===========================================================================
# SUBMIT
# ===========================================================================
if st.button(button_text, type="primary", disabled=not can_process):

    # -----------------------------------------------------------------------
    # BATCH
    # -----------------------------------------------------------------------
    if source_type == "📁 Batch CSV Upload":
        urls = st.session_state.get("batch_urls", [])
        csv_df = st.session_state.get("batch_df")
        url_column = st.session_state.get("batch_url_column")
        id_column = st.session_state.get("batch_id_column")

        results = []
        with st.spinner(f"Submitting {len(urls)} videos..."):
            for url in urls:
                custom_vid_id = None
                if id_column != "Auto-generate":
                    row = csv_df[csv_df[url_column] == url]
                    if not row.empty:
                        custom_vid_id = (
                            re.sub(r"[^\w\s-]", "", str(row[id_column].iloc[0]))
                            .strip()
                            .replace(" ", "_")[:50]
                        )

                url_type = detect_url_type(url)
                src_type = "youtube" if url_type == "youtube" else "box" if url_type == "box" else "direct"
                vid = custom_vid_id or generate_video_id(f"batch_{url}")

                _, error = submit_video(src_type, url, vid)
                results.append({"video_id": vid, "url": url, "source_type": src_type, "error": error or ""})

        successful = [r for r in results if not r["error"]]
        failed = [r for r in results if r["error"]]

        st.success(
            f"✅ Submitted {len(successful)} of {len(results)} videos. "
            f"Check **Manage Videos** to track progress."
        )
        if failed:
            st.warning(f"⚠️ {len(failed)} failed to submit:")
            for r in failed:
                st.text(f"• {r['video_id']}: {r['error']}")

        with st.expander("View submitted videos"):
            results_df = pd.DataFrame(results)
            st.dataframe(results_df)
            csv_buffer = io.StringIO()
            results_df.to_csv(csv_buffer, index=False)
            st.download_button(
                "Download Results CSV",
                csv_buffer.getvalue(),
                "batch_submit_results.csv",
                "text/csv",
            )

    # -----------------------------------------------------------------------
    # SINGLE VIDEO
    # -----------------------------------------------------------------------
    else:
        with st.spinner("Submitting..."):
            if source_type == "File Upload":
                vid, error = submit_video(
                    "upload", f"uploaded_file://{video_id}", video_id,
                    file_bytes=file_bytes, filename=uploaded_filename,
                )
            elif source_type == "YouTube":
                vid, error = submit_video("youtube", yt_url.strip(), video_id)
            elif source_type == "Direct URL" and detected_source_type == "box":
                vid, error = submit_video("box", media_url, video_id)
            else:  # Direct URL, generic
                vid, error = submit_video("direct", media_url, video_id)

        if error:
            st.error(f"❌ {error}")
        else:
            st.success(
                f"""
                ✅ **Submitted!**
                - Video ID: `{vid}`
                - Check the **Manage Videos** page to see when it's ready.
                """
            )
