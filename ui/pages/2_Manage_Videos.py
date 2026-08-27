"""
manage_videos.py - Manage Videos page for VANTAGE-AI
"""

import sys
sys.path.append("..")
import streamlit as st
import pandas as pd
import io
import time
from utils import (
    SEARCH_ENDPOINT,
    SEARCH_KEY,
    get_stored_videos,
    delete_video_by_id
)

APP_TITLE = "VANTAGE-AI: Video ANnotation, TAGging & Exploration"
st.title(APP_TITLE)
st.subheader("📚 Manage Stored Videos")
st.info("View, search, and manage all processed videos and their source URLs")

if not SEARCH_ENDPOINT or not SEARCH_KEY:
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

# ---------------------------------------------------------------------------
# URL coverage analysis
# ---------------------------------------------------------------------------
if st.button("📊 Analyze URL Data Coverage"):
    with st.spinner("Analyzing..."):
        all_videos = get_stored_videos(include_missing=True)

        with_urls    = [v for v in all_videos if v.get('source_url') and v.get('source_type') not in ('', 'unknown')]
        without_urls = [v for v in all_videos if not v.get('source_url') or v.get('source_type') in ('', 'unknown')]

        col1, col2, col3 = st.columns(3)
        col1.metric("Total Videos", len(all_videos))
        col2.metric("✅ With URL Data", len(with_urls),
                    f"{len(with_urls)/len(all_videos)*100:.1f}%" if all_videos else "0%")
        col3.metric("⚠️ Missing URL Data", len(without_urls),
                    f"{len(without_urls)/len(all_videos)*100:.1f}%" if all_videos else "0%")

        st.subheader("Breakdown by Source Type")
        type_counts = {}
        for v in all_videos:
            t = v.get('source_type') or 'unknown'
            type_counts[t] = type_counts.get(t, 0) + 1
        cols = st.columns(max(len(type_counts), 1))
        for i, (stype, count) in enumerate(sorted(type_counts.items())):
            icon = ("🎬" if stype == "youtube" else
                    "📦" if stype == "box"     else
                    "📄" if stype == "direct"  else
                    "📁" if stype == "upload"  else "❓")
            cols[i % len(cols)].metric(f"{icon} {stype}", count)

        if without_urls:
            with st.expander(f"Videos without URL data ({len(without_urls)})"):
                st.info("These were likely processed before URL tracking was enabled")
                for v in without_urls[:20]:
                    st.text(f"• {v.get('video_id')}")

st.markdown("---")

# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
st.subheader("Filter Videos")
col1, col2 = st.columns(2)
with col1:
    filter_video_id = st.text_input("Filter by Video ID (optional)")
with col2:
    filter_options = ["All", "With URL Data Only", "Missing URL Data Only",
                      "youtube", "box", "direct", "upload", "unknown"]
    filter_source_type = st.selectbox("Filter by Source Type", options=filter_options)

if st.button("🔍 Load Videos", type="primary"):
    with st.spinner("Retrieving videos..."):
        if filter_source_type == "Missing URL Data Only":
            all_videos = get_stored_videos(include_missing=True)
            videos = [v for v in all_videos
                      if not v.get('source_url') or v.get('source_type') in ('', 'unknown')]
            if filter_video_id.strip():
                videos = [v for v in videos
                          if filter_video_id.strip().lower() in v.get('video_id', '').lower()]
        elif filter_source_type == "With URL Data Only":
            all_videos = get_stored_videos(include_missing=True)
            videos = [v for v in all_videos
                      if v.get('source_url') and v.get('source_type') not in ('', 'unknown')]
            if filter_video_id.strip():
                videos = [v for v in videos
                          if filter_video_id.strip().lower() in v.get('video_id', '').lower()]
        else:
            source_type_arg = None if filter_source_type == "All" else filter_source_type
            videos = get_stored_videos(
                video_id=filter_video_id.strip() or None,
                source_type=source_type_arg,
                include_missing=True,
                limit=1000,
            )

        st.session_state.stored_videos_cache = videos
        st.session_state.videos_loaded = True
        st.success(f"Found {len(videos)} videos")

# ---------------------------------------------------------------------------
# Display + delete
# ---------------------------------------------------------------------------
if st.session_state.get('stored_videos_cache'):
    videos = st.session_state.stored_videos_cache

    if st.session_state.get('delete_success'):
        st.success("✅ Video deleted successfully")
        st.session_state.delete_success = False
    if st.session_state.get('delete_error'):
        st.error(f"❌ Failed to delete: {st.session_state.delete_error}")
        st.session_state.delete_error = None

    # Metrics row — dynamic, shows every source type actually present
    st.markdown("---")
    type_counts = {}
    for v in videos:
        t = v.get('source_type') or 'unknown'
        type_counts[t] = type_counts.get(t, 0) + 1

    all_types = sorted(type_counts.keys())
    cols = st.columns(max(len(all_types) + 1, 2))
    cols[0].metric("Total", len(videos))
    for i, stype in enumerate(all_types, 1):
        cols[i % len(cols)].metric(stype.capitalize(), type_counts.get(stype, 0))

    st.markdown("---")
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
            expanded=(stype == 'box')
        ):
            for i, video in enumerate(type_videos, 1):
                vid         = video.get('video_id', 'unknown')
                src_url     = video.get('source_url', '')
                processed   = video.get('processed_at', 'unknown')
                status_icon = "✅" if src_url else "⚠️"

                col_info, col_btn = st.columns([5, 1])

                with col_info:
                    st.write(f"**{status_icon} {i}. {vid}**")
                    st.caption(f"Processed: {processed}")
                    if src_url:
                        display_url = (src_url[:80] + "...") if len(src_url) > 80 else src_url
                        st.code(display_url)
                        if str(src_url).startswith('http'):
                            st.markdown(f"[Open Source ↗]({src_url})")
                    else:
                        st.warning("No source URL stored")

                with col_btn:
                    btn_key = f"del_{vid}_{i}_{stype}"
                    if st.button("🗑️", key=btn_key, help=f"Delete {vid}"):
                        st.session_state.pending_delete = vid
                        st.rerun()

                st.markdown("---")

    # ---------------------------------------------------------------------------
    # Export
    # ---------------------------------------------------------------------------
    st.markdown("---")
    if st.button("📥 Export to CSV"):
        export_df = pd.DataFrame([
            {
                'video_id':     v.get('video_id'),
                'source_type':  v.get('source_type') or 'unknown',
                'source_url':   v.get('source_url', ''),
                'has_url_data': bool(v.get('source_url')),
                'processed_at': v.get('processed_at', 'unknown'),
            }
            for v in videos
        ])
        buf = io.StringIO()
        export_df.to_csv(buf, index=False)
        st.download_button("Download CSV", buf.getvalue(), "video_list.csv", "text/csv")