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
LABEL_OVERRIDE_URL = os.environ.get("LABEL_OVERRIDE_URL", "")
APP_TITLE = "VANTAGE-AI: Video ANnotation, TAGging & Exploration"

st.set_page_config(page_title=APP_TITLE, layout="wide")

# =============================================================================
# SESSION STATE INITIALIZATION
# =============================================================================
defaults = {
    'index_schema_cache': None,
    'stored_videos_cache': None,
    'delete_success': False,
    'videos_loaded': False,
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
@st.cache_data(ttl=120)
def load_all_video_metadata() -> Dict[str, Dict]:
    """Fetch all videos with their source_url and return a dict keyed by video_id."""
    try:
        videos = get_stored_videos(limit=10000)
        return {v['video_id']: v for v in videos if v.get('video_id')}
    except Exception as e:
        print(f"load_all_video_metadata failed: {e}")
        st.error("Couldn't load video metadata — the search service didn't respond. Please try again in a moment.")
        return {}


@st.cache_data(ttl=120, show_spinner=False)
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


@st.cache_resource(max_entries=20, ttl=3600, show_spinner=False)
def load_box_audio(source_url: str) -> bytes:
    audio_bytes = fetch_box_audio_bytes(source_url)
    if not audio_bytes:
        raise RuntimeError("Box audio download failed")
    return audio_bytes


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


def call_override_api(payload: dict) -> dict:
    r = requests.post(
        LABEL_OVERRIDE_URL,
        json=payload,
        timeout=30,
        headers={"Content-Type": "application/json"},
    )
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
    return r.json() if r.text else {}


def get_overrides_for_video(video_id: str) -> dict:
    try:
        r = requests.get(LABEL_OVERRIDE_URL, params={"video_id": video_id}, timeout=30)
        if r.status_code >= 400:
            return {}
        return r.json() if r.text else {}
    except requests.exceptions.RequestException:
        return {}


@st.dialog("Preview AI Labels")
def preview_ai_labels_dialog(vid: str, seg: str, hit_idx: int, details: list, segment_overrides: dict):
    applied = [d for d in details if d.get("applied")]
    if not applied:
        st.caption("The AI doesn't currently apply any labels to this segment.")
    else:
        for d in sorted(applied, key=lambda d: d.get("name", "")):
            line = f"**{d.get('name', '')}**"
            if d.get("rationale"):
                line += f" — {d['rationale']}"
            st.markdown(line)

    manual_notes = []
    for ov in segment_overrides.values():
        name = ov.get("label_name", "")
        if ov.get("action") == "add":
            manual_notes.append(f"added **{name}**")
        elif ov.get("action") == "remove":
            manual_notes.append(f"removed **{name}**")
    if manual_notes:
        st.warning("Reapplying will discard these manual edits: " + ", ".join(manual_notes))

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Reapply", type="primary", use_container_width=True):
            try:
                result = call_override_api({
                    "video_id": vid,
                    "segment_id": seg,
                    "action": "reapply",
                    "reasoning": "",
                })
                st.session_state["search_hits"][hit_idx]["pred_labels"] = result.get("pred_labels", [])
                st.session_state[f"editing_labels_{vid}_{seg}"] = False
                st.rerun()
            except Exception as e:
                st.error(f"Couldn't reapply: {e}")
    with col2:
        if st.button("Close", use_container_width=True):
            st.rerun()


# =============================================================================
# RESULT CARD RENDERER
# =============================================================================
def render_hit(hit_idx: int, i: int, h: dict, metadata_cache: dict, label_by_name: dict, overrides_cache: dict) -> None:
    """Render a single search result card."""
    start_ms = h.get("start_ms", 0)
    end_ms   = h.get("end_ms",   0)
    vid      = h.get("video_id", "")
    seg      = h.get("segment_id", "")
    segment_key = h.get("segment_key") or f"{vid}_{seg}"
    score    = h.get("score", None)

    # Resolve source URL: search result first, then metadata cache
    source_url = h.get("source_url")
    if not source_url and vid in metadata_cache:
        source_url = metadata_cache[vid].get("source_url")

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
    score_str   = (f"  |  relevance={score:.3f}" if isinstance(score, (int, float))
                   else f"  |  relevance={score}") if score is not None else ""
    seg_str     = f"  |  seg={seg}" if seg else ""
    header      = f"{i}. [{ts_range}]  {display_vid}{seg_str}{score_str}"

    with st.expander(header, expanded=(i <= 3)):

        # ── Segment text ──────────────────────────────────────────────────
        st.write(h.get("text", ""))

        # ── Labels / rationale ───────────────────────────────────────────
        labels = h.get("pred_labels") or []
        edit_key = f"editing_labels_{segment_key}"
        gen_key = f"edit_gen_{segment_key}"

        if vid not in overrides_cache:
            overrides_cache[vid] = get_overrides_for_video(vid)
        video_overrides = overrides_cache[vid]
        segment_overrides = video_overrides.get(seg, {})
        manual_by_name = {
            ov["label_name"]: ov for ov in segment_overrides.values()
            if ov.get("action") == "add" and ov.get("label_name")
        }

        raw_details = h.get("pred_label_details")
        try:
            details = json.loads(raw_details) if isinstance(raw_details, str) else (raw_details or [])
        except (json.JSONDecodeError, TypeError):
            details = []

        label_cols = st.columns([6, 1])
        with label_cols[0]:
            st.write("**Labels:**", ", ".join(labels) if labels else "(none)")
        with label_cols[1]:
            editing = st.session_state.get(edit_key, False)
            if st.button("✕ Close" if editing else "Edit Labels", key=f"edit_toggle_{segment_key}"):
                st.session_state[edit_key] = not editing
                if not editing:
                    st.session_state[gen_key] = st.session_state.get(gen_key, 0) + 1
                st.rerun()

        rationale_items = []
        for name, ov in manual_by_name.items():
            if name in labels:
                rationale_items.append((name, ov.get("reasoning", ""), True))
        shown = {name for name, _, _ in rationale_items}
        for d in details:
            name = d.get("name")
            if name in labels and name not in shown and d.get("applied") and d.get("rationale"):
                rationale_items.append((name, d["rationale"], False))

        if rationale_items:
            with st.expander("Show rationale", expanded=False):
                for name, text, is_manual in rationale_items:
                    if is_manual:
                        st.markdown(f"**{name}** *(manually added)*: {text}")
                    else:
                        st.markdown(f"**{name}:** {text}")

        if st.session_state.get(edit_key):
            gen = st.session_state.get(gen_key, 0)
            ms_key = f"edit_labels_{segment_key}_{gen}"
            reason_key = f"edit_reason_{segment_key}_{gen}"
            pending_key = f"pending_diff_{segment_key}_{gen}"

            options = sorted(set(label_by_name) | set(labels))
            pending = st.session_state.get(pending_key)

            if pending is None:
                edited = st.multiselect(
                    "Edit labels", options, default=labels, key=ms_key, label_visibility="collapsed",
                )
                if st.button("See AI Labels", key=f"preview_ai_{segment_key}"):
                    preview_ai_labels_dialog(vid, seg, hit_idx, details, segment_overrides)
                added = [n for n in edited if n not in labels]
                removed = [n for n in labels if n not in edited]
                if added and removed:
                    st.session_state[gen_key] = st.session_state.get(gen_key, 0) + 1
                    st.rerun()
                elif added or removed:
                    st.session_state[pending_key] = {"added": added, "removed": removed}
                    st.rerun()
            else:
                added = pending["added"]
                removed = pending["removed"]
                st.session_state[ms_key] = [n for n in labels if n not in removed] + added
                st.multiselect(
                    "Edit labels", options, key=ms_key,
                    label_visibility="collapsed", disabled=True,
                )
                if st.button("See AI Labels", key=f"preview_ai_{segment_key}"):
                    preview_ai_labels_dialog(vid, seg, hit_idx, details, segment_overrides)

                with st.container(border=True):
                    if added:
                        st.write("**Adding:**", ", ".join(added))
                    if removed:
                        st.write("**Removing:**", ", ".join(removed))
                    reason = st.text_area("Reason (required)", key=reason_key)
                    edit_confirm_cols = st.columns(2)
                    with edit_confirm_cols[0]:
                        if st.button("Confirm", key=f"edit_confirm_{segment_key}", disabled=not reason.strip()):
                            try:
                                new_labels = labels
                                for name in removed:
                                    if name not in label_by_name:
                                        continue
                                    result = call_override_api({
                                        "video_id": vid,
                                        "segment_id": seg,
                                        "label_id": label_by_name[name]["label_id"],
                                        "action": "remove",
                                        "reasoning": reason.strip(),
                                    })
                                    new_labels = result.get("pred_labels", new_labels)
                                for name in added:
                                    result = call_override_api({
                                        "video_id": vid,
                                        "segment_id": seg,
                                        "label_id": label_by_name[name]["label_id"],
                                        "action": "add",
                                        "reasoning": reason.strip(),
                                    })
                                    new_labels = result.get("pred_labels", new_labels)
                                st.session_state["search_hits"][hit_idx]["pred_labels"] = new_labels
                                st.session_state[edit_key] = False
                                st.rerun()
                            except Exception as e:
                                st.error(f"Couldn't save label changes: {e}")
                    with edit_confirm_cols[1]:
                        if st.button("Cancel", key=f"edit_cancel_{segment_key}"):
                            st.session_state[edit_key] = False
                            st.rerun()

        st.divider()

        # ── Audio preview ─────────────────────────────────────────────────
        # Box URLs don't support time-based deep linking, so we fetch the
        # audio bytes server-side (via utils.fetch_box_audio_bytes) and use
        # st.audio with start_time. Only downloaded when the user clicks
        # "Load audio", so results render without waiting on Box. The bytes
        # are cached once per source URL for all sessions (load_box_audio),
        # so other segments from the same video don't re-download the file.
        if source_url and not supports_time and link_type.startswith("Box"):
            start_sec = max(0, int(start_ms // 1000))
            end_sec   = int(end_ms // 1000) if end_ms and end_ms > start_ms else None

            requested_key = f"audio_requested_{source_url}"
            audio_slot = st.empty()
            if not st.session_state.get(requested_key):
                if audio_slot.button("🔊 Load audio", key=f"load_audio_{i}_{vid}_{seg}"):
                    st.session_state[requested_key] = True
                    audio_slot.empty()

            if st.session_state.get(requested_key):
                try:
                    with st.spinner("Loading audio preview…"):
                        audio_bytes = load_box_audio(source_url)
                    with audio_slot.container():
                        st.audio(
                            audio_bytes,
                            format="audio/m4a",
                            start_time=start_sec,
                            end_time=end_sec,
                        )
                        st.caption(f"▶ Playing from {ms_to_ts(start_ms)} to {ms_to_ts(end_ms)}")
                except Exception:
                    st.session_state[requested_key] = False
                    audio_slot.warning("⚠ Could not load audio preview — open Box link below")

        # ── Link row ──────────────────────────────────────────────────────
        if start_link == "#":
            if link_type == "Internal storage (no public link)":
                st.info("📁 This video was uploaded directly — no playback link is available for it.")
            else:
                st.warning("No playback link available yet — try the sidebar's refresh cache button.")
        else:
            link_cols = st.columns([2, 2])

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


# =============================================================================
# SEARCH PAGE
# =============================================================================
def _render_how_to_use() -> None:
    with st.expander("How to use this app", expanded=False):
        st.markdown(
            """
            1. **Upload**: Open the **Upload** page and submit a file from your computer, a URL (YouTube, Box, or a direct link), or a CSV with one video URL per row for a batch upload.
            2. **Check on progress**: Use **Manage Videos** → **Pending Uploads** to see which videos are still transcribing, and **Browse & Manage** to view, delete, or export videos that are already done.
            3. **Create labels**: In **Label Management**, define your own labels (for example, vaccine skepticism or trust messaging), with an optional description and example passages. Adding or editing a label automatically queues it to be applied to every video's segments.
            4. **Check labeling accuracy** (optional): **Label Evaluation** is a separate tool for testing how well the AI's labeling matches your own judgment — upload a CSV of text you've manually labeled yourself, and it reports precision/recall/F1 per label.
            5. **Search and filter**: Return here to search by keyword, then optionally narrow results to one `video_id` or filter by predicted labels.
            6. **Inspect evidence**: Expand any result card to read the excerpt, review the AI's rationale for each applied label, and jump directly to the right timestamp in the original video.

            **Tips**
            - You can search with just labels (no text query) by selecting one or more labels in the sidebar.
            - Video metadata refreshes automatically every couple of minutes, or click the sidebar's refresh button for it immediately.
            """
        )


def render_search_page() -> None:
    st.title(APP_TITLE, anchor=False)
    st.subheader("Search Video Segments")

    # ── Sidebar ───────────────────────────────────────────────────────────
    with st.sidebar:
        st.header("Search Settings")
        mode = st.selectbox("Mode", ["keyword", "hybrid", "vector"], index=1)
        video_id_filter = st.text_input("Search by video_id", value="")
        label_names = get_label_names()
        selected_labels = st.multiselect("Filter by labels", label_names)
        if selected_labels:
            label_match = st.radio("Match labels", ["any", "all"], horizontal=True)
        else:
            label_match = "any"

        cache_size = len(st.session_state['video_metadata_cache'])
        if cache_size:
            st.caption(f"📦 {cache_size} videos in metadata cache")
        if st.button("🔄 Refresh cache"):
            st.cache_data.clear()
            st.session_state['video_metadata_cache'] = load_all_video_metadata()
            st.session_state['metadata_loaded'] = True
            st.rerun()

    # ── Search bar ────────────────────────────────────────────────────────
    PAGE_SIZE = 10

    q  = st.text_input("Query", value="", placeholder="e.g., measles misinformation")
    go = st.button("Search", type="primary", disabled=(not q.strip() and not selected_labels))
    if video_id_filter.strip() and not q.strip() and not selected_labels:
        st.caption("Add a keyword or select a label to search.")

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
        _render_how_to_use()
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
                print(f"Search failed: {e}")
                st.error("Search failed — the search service didn't respond. Please try again in a moment.")
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
        _render_how_to_use()
        return

    st.caption(f"Total: {total_count} | Page {page + 1} of {total_pages}")

    if not st.session_state.get('metadata_loaded') and any(not h.get("source_url") for h in hits):
        with st.spinner("Loading video metadata..."):
            st.session_state['video_metadata_cache'] = load_all_video_metadata()
            st.session_state['metadata_loaded'] = True
    metadata_cache = st.session_state['video_metadata_cache']
    label_by_name = {l["name"]: l for l in get_labels()}
    overrides_cache = {}

    for hit_idx, h in enumerate(hits):
        render_hit(hit_idx, page * PAGE_SIZE + hit_idx + 1, h, metadata_cache, label_by_name, overrides_cache)

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

    _render_how_to_use()


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