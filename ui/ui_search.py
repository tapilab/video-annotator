"""
ui_search.py - Main Streamlit entry point
Uses multipage navigation (pages folder) and shared utilities from utils.py
"""

import json
import os
import re
import requests
import streamlit as st
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Tuple, List
from dotenv import load_dotenv

from utils import ms_to_ts, SEARCH_FN_URL, get_stored_videos, build_video_link, get_box_audio_url, fetch_box_audio_bytes

load_dotenv()
MANAGE_LABELS_URL = os.environ.get("MANAGE_LABELS_URL", "")
APP_TITLE = "VANTAGE-AI: Video ANnotation, TAGging & Exploration"

st.set_page_config(page_title=APP_TITLE, layout="wide")

# =============================================================================
# SESSION STATE INITIALIZATION
# =============================================================================
defaults = {
    'yt_url_value': "",
    'batch_results': [],
    'batch_processing': False,
    'index_schema_cache': None,
    'stored_videos_cache': None,
    'url_fields_status': None,
    'debug_info': {},
    'video_to_delete': None,
    'delete_success': False,
    'videos_loaded': False,
    'debug_poll_url': None,
    'video_metadata_cache': {},
    'metadata_loaded': False,
    'pending_delete': None,
    'delete_error': None,
    'search_hits': [],
    'search_count': None,
    'search_page': 0,
    'search_params': None,
    'search_loading': False,
}
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value


# =============================================================================
# METADATA CACHE
# =============================================================================
@st.cache_data(ttl=600)
def load_all_video_metadata() -> Dict[str, Dict]:
    """Fetch all videos with their source_url and return a dict keyed by video_id."""
    try:
        videos = get_stored_videos(limit=10000)
        return {v['video_id']: v for v in videos if v.get('video_id')}
    except Exception as e:
        st.error(f"Failed to load video metadata: {e}")
        return {}


@st.cache_data(ttl=300, show_spinner=False)
def get_labels() -> list:
    if not MANAGE_LABELS_URL:
        return []
    try:
        r = requests.get(MANAGE_LABELS_URL, timeout=30)
        if r.status_code >= 400:
            return []
        return r.json().get("labels", [])
    except Exception:
        return []


def get_label_names() -> list:
    return [l["name"] for l in get_labels()]


# =============================================================================
# SEARCH API
# =============================================================================
def call_search_api(payload: dict) -> dict:
    r = requests.post(
        SEARCH_FN_URL,
        json=payload,
        timeout=60,
        headers={"Content-Type": "application/json"},
    )
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
    return r.json() if r.text else {}


# =============================================================================
# RESULT CARD RENDERER
# =============================================================================
def render_hit(i: int, h: dict, metadata_cache: dict) -> dict:
    """Render a single search result card."""
    start_ms = h.get("start_ms", 0)
    end_ms   = h.get("end_ms",   0)
    vid      = h.get("video_id", "")
    seg      = h.get("segment_id", "")
    score    = h.get("score", None)

    # Resolve source URL: search result first, then metadata cache
    source_url    = h.get("source_url")
    source_origin = "search"
    if not source_url and vid in metadata_cache:
        source_url    = metadata_cache[vid].get("source_url")
        source_origin = "cache"

    source_type = h.get("source_type")

    # Build start and end links
    start_link, link_type, supports_time = build_video_link(
        vid, start_ms, source_url, source_type
    )
    end_link, _, _ = build_video_link(
        vid, end_ms, source_url, source_type
    )

    # Expander header
    ts_range    = f"{ms_to_ts(start_ms)} → {ms_to_ts(end_ms)}"
    display_vid = vid if len(vid) < 28 else f"{vid[:25]}..."
    score_str   = (f"  |  score={score:.3f}" if isinstance(score, (int, float))
                   else f"  |  score={score}") if score is not None else ""
    seg_str     = f"  |  seg={seg}" if seg else ""
    header      = f"{i}. [{ts_range}]  {display_vid}{seg_str}{score_str}"

    with st.expander(header, expanded=(i <= 3)):

        # ── Segment text ──────────────────────────────────────────────────
        st.write(h.get("text", ""))

        # ── Labels / rationale ───────────────────────────────────────────
        labels = h.get("pred_labels") or []
        if labels:
            st.write("**Labels:**", ", ".join(labels))
            raw_details = h.get("pred_label_details")
            if raw_details:
                try:
                    details = json.loads(raw_details) if isinstance(raw_details, str) else raw_details
                    applied = [d for d in details if d.get("applied") and d.get("rationale")]
                    if applied:
                        with st.expander("Show rationale", expanded=False):
                            for d in applied:
                                st.markdown(f"**{d['name']}:** {d['rationale']}")
                except (json.JSONDecodeError, TypeError):
                    pass

        st.divider()

        # ── Audio preview ─────────────────────────────────────────────────
        # Box URLs don't support time-based deep linking, so we fetch the
        # audio bytes server-side (via utils.fetch_box_audio_bytes) and use
        # st.audio with start_time. Cached per source URL so multiple segments
        # from the same video don't re-download the file.
        if source_url and not supports_time and link_type.startswith("Box"):
            start_sec = max(0, int(start_ms // 1000))
            end_sec   = int(end_ms // 1000) if end_ms and end_ms > start_ms else None

            cache_key = f"box_bytes_{source_url}"
            if cache_key not in st.session_state:
                with st.spinner("Loading audio preview…"):
                    st.session_state[cache_key] = fetch_box_audio_bytes(source_url)

            audio_bytes = st.session_state.get(cache_key)
            if audio_bytes:
                st.audio(
                    audio_bytes,
                    format="audio/m4a",
                    start_time=start_sec,
                    end_time=end_sec,
                )
                st.caption(f"▶ Playing from {ms_to_ts(start_ms)} to {ms_to_ts(end_ms)}")
            else:
                st.warning("⚠ Could not load audio preview — open Box link below")

        # ── Link row ──────────────────────────────────────────────────────
        if start_link == "#":
            st.error("❌ No source URL stored — cannot generate playback link")
        else:
            link_cols = st.columns([2, 2, 3])

            with link_cols[0]:
                if link_type == "Box (download)":
                    play_label = "⬇️ Download Box file"
                elif link_type.startswith("Box"):
                    play_label = "📦 Open in Box viewer"
                else:
                    play_label = f"▶️ Play from {ms_to_ts(start_ms)}"
                st.markdown(f"**[{play_label}]({start_link})**", unsafe_allow_html=True)
                st.caption(f"*{link_type}*")

            with link_cols[1]:
                if end_ms and end_ms != start_ms:
                    st.info(f"⏱ **{ms_to_ts(start_ms)}** – **{ms_to_ts(end_ms)}**")
                else:
                    st.empty()

            with link_cols[2]:
                if source_url:
                    display_url = (source_url[:45] + "…") if len(source_url) > 45 else source_url
                    st.caption(f"🔗 [{display_url}]({source_url})")
                    st.caption(f"from {source_origin}")
                else:
                    st.caption("❌ No source URL")

    return {
        "source_origin": source_origin if source_url else "missing",
        "link_type":     link_type,
    }


# =============================================================================
# SEARCH PAGE
# =============================================================================
def render_search_page() -> None:
    st.title(APP_TITLE)
    st.caption("Upload videos, define annotation labels, run LLM labeling, and search across segment-level results.")

    with st.expander("How to use this app", expanded=False):
        st.markdown(
            """
            1. **Upload**: Open the **Upload** page and submit either YouTube links or a CSV file with one video URL per row.
            2. **Manage videos**: Use **Manage Videos** to confirm indexing status and clean up records when needed.
            3. **Create labels**: In **Label Management**, define your own annotation goals (for example, vaccine skepticism or trust messaging).
            4. **Run labeling**: Use **Label Evaluation** to apply the LLM annotator to indexed segments for each selected label.
            5. **Search and filter**: Return here to search by keyword, filter by `video_id`, and filter by predicted labels.
            6. **Inspect evidence**: Expand any result card to read the excerpt, review confidence/rationale, and jump directly to timestamps.

            **Tips**
            - You can search with just labels (no text query) by selecting one or more labels in the sidebar.
            - For hybrid search, keep `k` roughly 4x `top` for stronger recall.
            - Use the cache refresh button if new videos were recently ingested.
            """
        )

    # Load metadata on first run
    if not st.session_state.get('metadata_loaded'):
        with st.spinner("Loading video metadata..."):
            st.session_state['video_metadata_cache'] = load_all_video_metadata()
            st.session_state['metadata_loaded'] = True

    # ── Sidebar ───────────────────────────────────────────────────────────
    with st.sidebar:
        st.header("Settings")
        mode = st.selectbox("Mode", ["keyword", "hybrid", "vector"], index=1)
        video_id_filter = st.text_input("Filter by video_id (optional)", value="")
        label_names = get_label_names()
        selected_labels = st.multiselect("Filter by labels (optional)", label_names)
        if selected_labels:
            label_match = st.radio("Match labels", ["any", "all"], horizontal=True)
        else:
            label_match = "any"

        cache_size = len(st.session_state['video_metadata_cache'])
        st.caption(f"📦 {cache_size} videos in metadata cache")
        if st.button("🔄 Refresh cache"):
            st.cache_data.clear()
            st.session_state['video_metadata_cache'] = load_all_video_metadata()
            st.session_state['metadata_loaded'] = True
            st.rerun()

        st.caption("Tip: keep k ~ 4×top for hybrid.")

    # ── Search bar ────────────────────────────────────────────────────────
    PAGE_SIZE = 10

    q  = st.text_input("Query", value="", placeholder="e.g., measles misinformation")
    go = st.button("Search", type="primary", disabled=(not q.strip() and not selected_labels))

    if go:
        params = {"q": q.strip(), "mode": mode, "top": PAGE_SIZE}
        if mode in ("hybrid", "vector"):
            params["k"] = None
        if video_id_filter.strip():
            params["video_id"] = video_id_filter.strip()
        if selected_labels:
            params["labels"] = selected_labels
            params["label_match"] = label_match
        st.session_state['search_params']  = params
        st.session_state['search_page']    = 0
        st.session_state['search_hits']    = []
        st.session_state['search_count']   = None
        st.session_state['search_loading'] = True

    params = st.session_state.get('search_params')
    if not params:
        return

    page = st.session_state['search_page']

    # ── Fetch ─────────────────────────────────────────────────────────────
    if st.session_state['search_loading']:
        with st.spinner("Searching…"):
            skip = page * PAGE_SIZE
            payload = {**params, "skip": skip}
            if payload.get("k") is None:
                payload["k"] = min(skip + PAGE_SIZE * 4, 200)
            try:
                data = call_search_api(payload)
            except Exception as e:
                st.error(f"Search failed: {e}")
                st.session_state['search_loading'] = False
                st.stop()
        st.session_state['search_hits']    = [h for h in data.get("hits", []) if h.get("video_id") and h.get("text")]
        st.session_state['search_count']   = data.get("count") or 0
        st.session_state['search_loading'] = False
        st.rerun()

    # ── Render ────────────────────────────────────────────────────────────
    hits        = st.session_state['search_hits']
    total_count = st.session_state['search_count'] or 0
    total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)

    if not hits and page == 0:
        st.info("No results found.")
        return

    st.caption(f"Total: {total_count} | Page {page + 1} of {total_pages}")

    metadata_cache = st.session_state['video_metadata_cache']
    source_stats   = {"from_search": 0, "from_cache": 0, "missing": 0}
    type_counts    = {}

    for i, h in enumerate(hits, start=page * PAGE_SIZE + 1):
        stats  = render_hit(i, h, metadata_cache)
        origin = stats["source_origin"]
        source_stats["missing" if origin == "missing" else
                     "from_cache" if origin == "cache" else "from_search"] += 1
        lt = stats["link_type"]
        type_counts[lt] = type_counts.get(lt, 0) + 1

    # ── Pagination ────────────────────────────────────────────────────────
    st.divider()
    nav_cols = st.columns([1, 2, 1])
    with nav_cols[0]:
        if page > 0:
            if st.button("← Previous"):
                st.session_state['search_page']    -= 1
                st.session_state['search_loading']  = True
                st.rerun()
    with nav_cols[1]:
        st.caption(f"Page {page + 1} of {total_pages}  ({total_count} results)")
    with nav_cols[2]:
        if page < total_pages - 1:
            if st.button("Next →"):
                st.session_state['search_page']    += 1
                st.session_state['search_loading']  = True
                st.rerun()

    # ── Summary footer ────────────────────────────────────────────────────
    col1, col2, col3 = st.columns(3)
    with col1:
        st.caption(f"From search result: {source_stats['from_search']}")
    with col2:
        st.caption(f"From metadata cache: {source_stats['from_cache']}")
    with col3:
        if source_stats['missing'] > 0:
            st.caption(f"⚠ Missing source URL: {source_stats['missing']}")
        else:
            st.caption("✅ All results have source URLs")

    type_summary = ", ".join(f"{k}: {v}" for k, v in type_counts.items())
    st.caption(f"Link types: {type_summary}")


# =============================================================================
# MULTIPAGE NAVIGATION
# =============================================================================
pg_search = st.Page(render_search_page,                       title="Search",             icon="🔎", default=True)
pg_upload = st.Page("pages/1_Upload.py",                      title="Upload",             icon="⬆️")
pg_manage = st.Page("pages/2_Manage_Videos.py",               title="Manage Videos",      icon="📚")
pg_labels = st.Page("pages/3_Label_Management.py",            title="Label Management",   icon="🏷️")
pg_eval   = st.Page("pages/4_Label_Evaluation.py",            title="Label Evaluation",   icon="📊")
pg_diag   = st.Page("pages/5_System_Diagnostics.py",          title="System Diagnostics", icon="⚙️")

st.navigation([pg_search, pg_upload, pg_manage, pg_labels, pg_eval, pg_diag]).run()