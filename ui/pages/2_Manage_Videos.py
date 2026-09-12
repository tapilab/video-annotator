"""
manage_videos.py - Manage Videos page for VANTAGE-AI
"""

import sys
sys.path.append("..")
import streamlit as st
import pandas as pd
import io
import os
import requests
from utils import (
    SEARCH_ENDPOINT,
    SEARCH_ADMIN_KEY,
    get_stored_videos,
    delete_video_by_id,
    get_pending_uploads,
    save_pending_uploads,
    format_timestamp,
)

TRANSCRIBE_URL = os.environ.get("TRANSCRIBE_URL", "")
EMBED_INDEX_URL = os.environ.get("EMBED_INDEX_URL", "")

APP_TITLE = "VANTAGE-AI: Video ANnotation, TAGging & Exploration"
st.title(APP_TITLE, anchor=False)
st.subheader("Manage Stored Videos")

if not SEARCH_ENDPOINT or not SEARCH_ADMIN_KEY:
    st.error("Azure Search not configured. Cannot retrieve video list.")
    st.stop()

# ---------------------------------------------------------------------------
# Process any pending delete BEFORE rendering
# ---------------------------------------------------------------------------
if st.session_state.get('pending_delete'):
    vid_to_delete = st.session_state.pending_delete
    st.session_state.pending_delete = None
    with st.spinner(f"Deleting {vid_to_delete}..."):
        success = delete_video_by_id(vid_to_delete)
    if success:
        st.session_state.stored_videos_cache = [
            v for v in (st.session_state.get('stored_videos_cache') or [])
            if v.get('video_id') != vid_to_delete
        ]
        st.session_state.delete_success = True
    else:
        st.session_state.delete_error = vid_to_delete

tab_browse, tab_pending = st.tabs(["Browse & Manage", "Pending Uploads"])

# ---------------------------------------------------------------------------
# Browse, filter, delete, export
# ---------------------------------------------------------------------------
with tab_browse:
    st.subheader("Search Videos")
    filter_video_id = st.text_input("Search by Video ID")

    load_clicked = st.button("Submit", type="primary")

    # Auto-load every video the first time this tab is visited, so there's
    # already something to look through instead of an empty page.
    if load_clicked or not st.session_state.get('videos_loaded'):
        with st.spinner("Retrieving videos..."):
            videos = get_stored_videos(
                video_id=filter_video_id.strip() or None,
                source_type=None,
                include_missing=True,
                limit=1000,
            )

            st.session_state.stored_videos_cache = videos
            if load_clicked and not videos:
                st.warning("No videos found matching that filter.")
            st.session_state.videos_loaded = True

    if st.session_state.get('stored_videos_cache'):
        videos = st.session_state.stored_videos_cache

        if st.session_state.get('delete_success'):
            st.success("✅ Video deleted successfully")
            st.session_state.delete_success = False
        if st.session_state.get('delete_error'):
            st.error(f"❌ Failed to delete: {st.session_state.delete_error}")
            st.session_state.delete_error = None

        st.divider()

        # Metrics row - dynamic, shows every source type actually present
        type_counts = {}
        for v in videos:
            t = v.get('source_type') or 'unknown'
            type_counts[t] = type_counts.get(t, 0) + 1

        all_types = sorted(type_counts.keys())
        cols = st.columns(max(len(all_types) + 1, 2))
        cols[0].metric("Total", len(videos))
        for i, stype in enumerate(all_types, 1):
            cols[i % len(cols)].metric(stype.capitalize(), type_counts.get(stype, 0))

        st.divider()
        st.subheader("Video List")

        # Group by source type
        videos_by_type: dict = {}
        for v in videos:
            stype = v.get('source_type') or 'unknown'
            videos_by_type.setdefault(stype, []).append(v)

        # Known types first, then any unexpected ones
        known_order = ['youtube', 'box', 'direct', 'upload', 'unknown']
        other_types = [t for t in videos_by_type if t not in known_order]

        for stype in known_order + other_types:
            if stype not in videos_by_type:
                continue
            type_videos = videos_by_type[stype]
            icon = ("🎬" if stype == "youtube" else
                    "📦" if stype == "box"     else
                    "📄" if stype == "direct"  else
                    "📁" if stype == "upload"  else "❓")

            with st.expander(
                f"{icon} {stype.upper()} ({len(type_videos)} videos)",
                expanded=False
            ):
                for i, video in enumerate(type_videos, 1):
                    vid         = video.get('video_id', 'unknown')
                    src_url     = video.get('source_url', '')
                    processed   = format_timestamp(video.get('processed_at', 'unknown'))

                    col_info, col_btn = st.columns([5, 1])

                    with col_info:
                        st.write(f"**{i}. {vid}**")
                        st.caption(f"Processed: {processed}")
                        if src_url:
                            display_url = (src_url[:80] + "...") if len(src_url) > 80 else src_url
                            st.code(display_url)
                            if str(src_url).startswith('http'):
                                st.markdown(f"[Open Source ↗]({src_url})")

                    with col_btn:
                        btn_key = f"del_{vid}_{i}_{stype}"
                        confirm_key = f"confirm_delete_{vid}"
                        if st.session_state.get(confirm_key):
                            if st.button("✅ Confirm", key=f"confirm_{btn_key}"):
                                st.session_state[confirm_key] = False
                                st.session_state.pending_delete = vid
                                st.rerun()
                            if st.button("✖️ Cancel", key=f"cancel_{btn_key}"):
                                st.session_state[confirm_key] = False
                                st.rerun()
                        else:
                            if st.button("🗑️", key=btn_key, help=f"Delete {vid}"):
                                st.session_state[confirm_key] = True
                                st.rerun()

                    st.divider()

        st.divider()
        if st.button("Export to CSV"):
            export_df = pd.DataFrame([
                {
                    'video_id':     v.get('video_id'),
                    'source_type':  v.get('source_type') or 'unknown',
                    'source_url':   v.get('source_url', ''),
                    'processed_at': v.get('processed_at', 'unknown'),
                }
                for v in videos
            ])
            buf = io.StringIO()
            export_df.to_csv(buf, index=False)
            st.download_button("Download CSV", buf.getvalue(), "video_list.csv", "text/csv")

# ---------------------------------------------------------------------------
# Pending uploads - videos submitted from the Upload page that are still
# transcribing or waiting to be indexed. This is what replaced the old
# progress bar: check back here instead of watching a live status screen.
# ---------------------------------------------------------------------------
with tab_pending:
    pending = get_pending_uploads()

    if not pending:
        st.caption("No videos currently processing.")
    else:
        if st.button("Check Pending Uploads"):
            updated_pending = dict(pending)
            for vid, info in pending.items():
                try:
                    r = requests.post(
                        TRANSCRIBE_URL,
                        json={"job_url": info["job_url"], "video_id": vid},
                        timeout=60,
                    )
                    resp = r.json() if r.text else {}
                    status = resp.get("status")

                    if status == "Succeeded":
                        segments_blob = resp.get("segments_blob")
                        idx_r = requests.post(
                            EMBED_INDEX_URL,
                            json={
                                "segments_blob": segments_blob,
                                "source_url": info.get("source_url", ""),
                                "source_type": info.get("source_type", "unknown"),
                            },
                            timeout=180,
                        )
                        if idx_r.status_code < 400:
                            del updated_pending[vid]
                            st.success(f"✅ {vid} finished processing and is now searchable.")
                        else:
                            st.error(f"❌ {vid} transcribed but indexing failed: {idx_r.text}")
                    elif status == "Failed":
                        st.error(f"❌ {vid} failed: {resp}")
                        del updated_pending[vid]
                    else:
                        st.info(f"⏳ {vid}: still {status or 'processing'}")
                except Exception as e:
                    print(f"Pending upload check failed for {vid}: {e}")
                    st.warning(f"Could not check {vid} — the service didn't respond. Try again in a moment.")

            save_pending_uploads(updated_pending)
            st.rerun()

        for vid, info in pending.items():
            st.text(f"• {vid} — submitted {format_timestamp(info.get('submitted_at', 'unknown'))} ({info.get('source_type', 'unknown')})")
