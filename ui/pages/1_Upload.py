"""
upload_transcribe.py - Upload & Transcribe page for VANTAGE-AI
"""

import sys
sys.path.append("..")          # allows import from parent directory
import streamlit as st
import time
import pandas as pd
import io
import tempfile
import re
from typing import Tuple, Optional
from utils import (
    AZURE_STORAGE_KEY,
    SPEECH_KEY,
    POLL_SECONDS,
    generate_video_id,
    check_yt_dlp,
    detect_url_type,
    upload_to_azure_blob_sdk,
    upload_to_azure_blob_fixed,
    submit_transcription_direct,
    poll_transcription_operation,
    get_transcription_from_result,
    process_transcription_to_segments,
    save_segments_to_blob,
    index_segments_direct,
    ms_to_ts,
    process_single_video,
    download_youtube_audio,
    download_box_audio,
)

APP_TITLE = "VANTAGE-AI: Video ANnotation, TAGging & Exploration"
st.title(APP_TITLE)
st.subheader("Upload Video for Transcription")

# Check Azure configuration
azure_configured = bool(AZURE_STORAGE_KEY) and bool(SPEECH_KEY)
if not azure_configured:
    st.error("⚠️ Azure Storage and Speech keys required. Check .env file.")

# Source selection
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
original_box_url = None   # preserved for storing in index when Box is downloaded

# ---------------------------------------------------------------------------
# File Upload
# ---------------------------------------------------------------------------
if source_type == "File Upload":
    if not azure_configured:
        st.info("Please configure Azure Storage to enable file upload")
    else:
        uploaded_file = st.file_uploader(
            "Choose video/audio file",
            type=["mp4", "avi", "mov", "mkv", "m4a", "mp3", "wav"],
            accept_multiple_files=False,
        )
        if uploaded_file:
            st.success(f"📁 {uploaded_file.name} ({uploaded_file.size / 1024 / 1024:.1f} MB)")
            file_bytes = uploaded_file.getvalue()
            video_id = generate_video_id(uploaded_file.name)
            detected_source_type = "upload"
            st.info("File ready for upload")

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
        value=st.session_state.yt_url_value,
        key="yt_url_input",
    )

    if yt_url != st.session_state.yt_url_value:
        st.session_state.yt_url_value = yt_url
        try:
            st.rerun()
        except Exception:
            pass

    if not check_yt_dlp():
        st.error(
            "yt-dlp is not installed. Add `yt-dlp` to requirements.txt "
            "and redeploy the application."
        )
    elif yt_url and yt_url.strip():
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
            import traceback
            st.error(traceback.format_exc())

# ---------------------------------------------------------------------------
# Custom Video ID (single-video modes only)
# ---------------------------------------------------------------------------
custom_id = st.text_input("Custom Video ID (optional)")
if custom_id.strip() and source_type != "📁 Batch CSV Upload":
    video_id = custom_id.strip()

# ---------------------------------------------------------------------------
# Enable / disable the process button
# ---------------------------------------------------------------------------
can_process = False
if source_type == "File Upload":
    can_process = file_bytes is not None and azure_configured
elif source_type == "Direct URL":
    can_process = bool(media_url) and len(str(media_url).strip()) > 0
elif source_type == "YouTube":
    yt_url_to_check = st.session_state.get("yt_url_value", "")
    can_process = len(str(yt_url_to_check).strip()) > 0 and check_yt_dlp()
elif source_type == "📁 Batch CSV Upload":
    can_process = (
        bool(st.session_state.get("batch_urls"))
        and len(st.session_state.get("batch_urls", [])) > 0
        and azure_configured
        and not st.session_state.get("batch_processing", False)
    )

button_text = "🚀 Start Transcription"
if source_type == "📁 Batch CSV Upload":
    count = len(st.session_state.get("batch_urls", []))
    button_text = f"🚀 Process {count} Videos from CSV"

# ===========================================================================
# MAIN PROCESSING
# ===========================================================================
if st.button(button_text, type="primary", disabled=not can_process):

    # -----------------------------------------------------------------------
    # BATCH PROCESSING
    # -----------------------------------------------------------------------
    if source_type == "📁 Batch CSV Upload":
        st.session_state.batch_processing = True
        st.session_state.batch_results = []

        urls = st.session_state.get("batch_urls", [])
        csv_df = st.session_state.get("batch_df")
        url_column = st.session_state.get("batch_url_column")
        id_column = st.session_state.get("batch_id_column")

        total = len(urls)
        st.info(f"Starting batch processing of {total} videos...")

        overall_progress = st.progress(0)
        status_text = st.empty()
        results_container = st.container()

        results = []
        for idx, url in enumerate(urls, 1):
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
            src_type = (
                "youtube" if url_type == "youtube"
                else "box" if url_type == "box"
                else "direct"
            )

            result = process_single_video(
                url=url,
                custom_id=custom_vid_id,
                source_type=src_type,
                progress_bar=overall_progress,
                status_text=status_text,
                overall_progress=(idx, total),
            )

            results.append(result)
            st.session_state.batch_results = results

            overall_progress.progress(int((idx / total) * 100))

            with results_container:
                if result["status"] == "success":
                    url_stored = (
                        "✅ URL saved" if result.get("url_stored") else "⚠️ URL not stored"
                    )
                    st.success(
                        f"✅ [{idx}/{total}] {result['video_id']}: "
                        f"{result['segments_count']} segments ({url_stored})"
                    )
                else:
                    error_msg = (result.get("error") or "Unknown error")[:200]
                    st.error(f"❌ [{idx}/{total}] Failed: {error_msg}...")

            time.sleep(1)  # basic rate limiting

        overall_progress.progress(100)
        status_text.text("Batch processing complete!")

        successful = [r for r in results if r["status"] == "success"]
        failed = [r for r in results if r["status"] == "failed"]

        st.markdown("---")
        st.subheader("📊 Batch Processing Summary")
        col1, col2, col3 = st.columns(3)
        col1.metric("Total", total)
        col2.metric(
            "Successful",
            len(successful),
            f"{len(successful)/total*100:.1f}%" if total else "0%",
        )
        col3.metric(
            "Failed",
            len(failed),
            f"{len(failed)/total*100:.1f}%" if total else "0%",
        )

        with st.expander("View Detailed Results"):
            results_df = pd.DataFrame(
                [
                    {
                        "Video ID": r["video_id"],
                        "URL": (
                            r["url"][:50] + "..."
                            if len(r["url"]) > 50
                            else r["url"]
                        ),
                        "Source Type": r.get("source_type", "unknown"),
                        "Status": r["status"],
                        "Segments": r.get("segments_count", 0),
                        "URL Stored": r.get("url_stored", False),
                        "Indexing": r.get("index_status", "N/A"),
                        "Error": (
                            (r.get("error", "")[:100] + "...")
                            if r.get("error")
                            else ""
                        ),
                    }
                    for r in results
                ]
            )
            st.dataframe(results_df)
            csv_buffer = io.StringIO()
            results_df.to_csv(csv_buffer, index=False)
            st.download_button(
                "Download Results CSV",
                csv_buffer.getvalue(),
                "batch_results.csv",
                "text/csv",
            )

        if successful:
            st.info("💡 **Search processed videos:**")
            video_ids = [r["video_id"] for r in successful[:5]]
            st.code(f"video_id:({' OR '.join(video_ids)})")

        st.session_state.batch_processing = False

    # -----------------------------------------------------------------------
    # SINGLE VIDEO PROCESSING
    # -----------------------------------------------------------------------
    else:
        progress_bar = st.progress(0)
        status = st.empty()

        try:
            # -----------------------------------------------------------
            # File Upload → upload bytes to Azure Blob
            # -----------------------------------------------------------
            if source_type == "File Upload" and file_bytes:
                progress_bar.progress(10)
                status.text("Uploading to Azure Blob...")

                blob_name = f"upload_{video_id}_{int(time.time())}.m4a"
                sas_url, error = upload_to_azure_blob_sdk(file_bytes, blob_name)
                if error and ("not installed" in error or "SDK" in error):
                    sas_url, error = upload_to_azure_blob_fixed(file_bytes, blob_name)
                if error:
                    raise Exception(error)

                media_url = sas_url
                progress_bar.progress(50)

            # -----------------------------------------------------------
            # YouTube → yt-dlp download → upload to Azure Blob
            # -----------------------------------------------------------
            elif source_type == "YouTube":
                yt_url = st.session_state.get("yt_url_value", "")
                if not yt_url or not yt_url.strip():
                    raise Exception("YouTube URL is empty")

                with tempfile.TemporaryDirectory() as tmpdir:
                    progress_bar.progress(10)
                    status.text("Downloading from YouTube...")

                    output_path = f"{tmpdir}/youtube_{video_id}.m4a"
                    downloaded_path, error = download_youtube_audio(
                        yt_url.strip(), output_path
                    )
                    if error:
                        raise Exception(error)

                    progress_bar.progress(50)
                    status.text("Uploading to Azure Blob...")

                    with open(downloaded_path, "rb") as f:
                        file_bytes = f.read()

                    blob_name = f"youtube_{video_id}_{int(time.time())}.m4a"
                    sas_url, error = upload_to_azure_blob_sdk(file_bytes, blob_name)
                    if error and "not installed" in error:
                        sas_url, error = upload_to_azure_blob_fixed(file_bytes, blob_name)
                    if error:
                        raise Exception(error)

                    media_url = sas_url
                    progress_bar.progress(75)

            # -----------------------------------------------------------
            # Box URL → download via requests → upload to Azure Blob
            # Preserves the original Box URL for display / search links.
            # -----------------------------------------------------------
            elif source_type == "Direct URL" and detected_source_type == "box":
                original_box_url = media_url   # keep for index storage

                with tempfile.TemporaryDirectory() as tmpdir:
                    progress_bar.progress(10)
                    status.text("Downloading from Box...")

                    output_path = f"{tmpdir}/box_{video_id}.m4a"
                    downloaded_path, error = download_box_audio(
                        original_box_url, output_path
                    )
                    if error:
                        raise Exception(f"Box download failed: {error}")

                    progress_bar.progress(50)
                    status.text("Uploading to Azure Blob...")

                    with open(downloaded_path, "rb") as f:
                        box_file_bytes = f.read()

                    blob_name = f"box_{video_id}_{int(time.time())}.m4a"
                    sas_url, error = upload_to_azure_blob_sdk(box_file_bytes, blob_name)
                    if error and "not installed" in error:
                        sas_url, error = upload_to_azure_blob_fixed(
                            box_file_bytes, blob_name
                        )
                    if error:
                        raise Exception(f"Azure upload failed: {error}")

                    # SAS URL goes to Speech API; original Box URL goes to the index
                    media_url = sas_url
                    progress_bar.progress(75)

            # -----------------------------------------------------------
            # Generic Direct URL → pass straight to Speech API
            # -----------------------------------------------------------
            # media_url is already set from the input widget; nothing extra needed.

            if not media_url:
                raise Exception("No media URL available")

            # -----------------------------------------------------------
            # Submit to Azure Speech-to-Text
            # -----------------------------------------------------------
            status.text("Submitting to Azure Speech-to-Text...")
            result = submit_transcription_direct(video_id, media_url)
            operation_url = result.get("operation_url")
            if not operation_url:
                raise Exception("No operation URL returned")

            # -----------------------------------------------------------
            # Poll until complete
            # -----------------------------------------------------------
            max_polls = 120
            transcription_data = None

            for i in range(max_polls):
                time.sleep(POLL_SECONDS)
                poll_result = poll_transcription_operation(operation_url)
                poll_status = poll_result.get("status", "unknown")

                progress = min(75 + int((i / max_polls) * 20), 95)
                progress_bar.progress(progress)
                status.text(
                    f"Transcribing... ({i * POLL_SECONDS // 60} min) "
                    f"— Status: {poll_status}"
                )

                if poll_status.lower() == "succeeded":
                    transcription_data = get_transcription_from_result(poll_result)
                    break
                elif poll_status.lower() == "failed":
                    raise Exception(
                        "Transcription failed: "
                        + poll_result.get("properties", {})
                        .get("error", {})
                        .get("message", "Unknown error")
                    )

            if not transcription_data:
                raise Exception("Transcription timed out")

            # -----------------------------------------------------------
            # Segment, save, index
            # -----------------------------------------------------------
            progress_bar.progress(98)
            status.text("Processing segments and indexing...")

            segments = process_transcription_to_segments(transcription_data, video_id)
            save_segments_to_blob(video_id, segments)

            # Determine the URL to store in the search index.
            # Always use the original user-facing URL, never an internal SAS URL.
            if source_type == "YouTube":
                original_url = st.session_state.get("yt_url_value", "")
            elif source_type == "Direct URL" and detected_source_type == "box":
                original_url = original_box_url          # Box viewer/shared link
            elif source_type == "Direct URL":
                original_url = url_input.strip()         # whatever the user typed
            elif source_type == "File Upload":
                original_url = f"uploaded_file://{video_id}"
            else:
                original_url = None

            index_result = index_segments_direct(
                video_id,
                segments,
                source_url=original_url,
                source_type=detected_source_type,
            )

            url_stored_msg = (
                "✅ Source URL stored"
                if index_result.get("source_url_stored")
                else "⚠️ URL storage not available"
            )

            progress_bar.progress(100)
            status.text("Complete!")

            st.success(
                f"""
                ✅ **Transcription Complete!**
                - Video ID: `{video_id}`
                - Segments: {len(segments)}
                - Source Type: {detected_source_type}
                - Indexed: {index_result.get('indexed', 0)} documents
                - {url_stored_msg}
                """
            )

            if original_url and not original_url.startswith("uploaded_file://"):
                st.info(f"**Original Source:** [{original_url}]({original_url})")

            st.code(f"Search: video_id:{video_id}")

            with st.expander("View first 5 segments"):
                for seg in segments[:5]:
                    st.write(
                        f"**{ms_to_ts(seg['start_ms'])} – {ms_to_ts(seg['end_ms'])}:** "
                        f"{seg['text'][:100]}..."
                    )

        except Exception as e:
            st.error(f"❌ Error: {str(e)}")
            st.exception(e)