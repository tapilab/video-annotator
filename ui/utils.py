"""
utils.py - Shared helpers for the VANTAGE-AI Streamlit UI

Video ingestion (downloading, transcribing, embedding, and indexing) is
handled by the Azure Functions backend (StageMedia, TranscribeHttp,
EmbedAndIndex) - this file only holds what the UI itself still needs
directly: looking up and displaying already-processed videos, and
tracking uploads that are still being processed.
"""

import os
import requests
import streamlit as st
import json
import re
import hashlib
from typing import Optional, Dict, Any, Tuple, List
from pathlib import Path
from dotenv import load_dotenv
from azure.storage.blob import BlobServiceClient

load_dotenv()

__all__ = [
    "SEARCH_FN_URL",
    "SEARCH_ENDPOINT", "SEARCH_ADMIN_KEY", "SEARCH_INDEX_NAME",
    "AZURE_STORAGE_ACCOUNT", "AZURE_STORAGE_KEY", "PENDING_CONTAINER",
    "ms_to_ts", "ms_to_seconds", "detect_url_type",
    "debug_check_index_schema", "get_index_schema",
    "get_stored_videos", "delete_video_by_id", "get_source_url_for_video",
    "generate_video_id", "get_box_audio_url", "fetch_box_audio_bytes",
    "build_video_link", "get_pending_uploads", "save_pending_uploads",
]

# =============================================================================
# CONFIGURATION
# =============================================================================
SEARCH_FN_URL = os.environ.get("SEARCH_FN_URL", "")

SEARCH_ENDPOINT = os.environ.get("SEARCH_ENDPOINT")
SEARCH_ADMIN_KEY = os.environ.get("SEARCH_ADMIN_KEY")
SEARCH_INDEX_NAME = os.environ.get("SEARCH_INDEX_NAME", "segments")

AZURE_STORAGE_ACCOUNT = os.environ.get("AZURE_STORAGE_ACCOUNT", "storagevideoannotator")
AZURE_STORAGE_KEY = os.environ.get("AZURE_STORAGE_KEY", "")
PENDING_CONTAINER = os.environ.get("PENDING_CONTAINER", "uploads")

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def ms_to_ts(ms: int) -> str:
    s = max(0, int(ms // 1000))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def ms_to_seconds(ms: int) -> int:
    return max(0, int(ms // 1000))


def detect_url_type(url: str) -> str:
    """Classify a URL into: 'youtube', 'box', 'direct', or 'unknown'."""
    if not url:
        return "unknown"
    url_lower = str(url).lower().strip()

    youtube_patterns = [
        r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com|youtu\.be)',
        r'youtube\.com\/watch\?v=',
        r'youtu\.be\/',
        r'youtube\.com\/shorts\/'
    ]
    for pattern in youtube_patterns:
        if re.search(pattern, url_lower):
            return "youtube"

    if "box.com" in url_lower or "boxcloud.com" in url_lower:
        return "box"

    media_extensions = ['.mp4', '.m4a', '.mp3', '.wav', '.mov', '.avi', '.mkv', '.webm']
    if any(url_lower.endswith(ext) for ext in media_extensions):
        return "direct"
    cloud_patterns = ['drive.google.com', 'dropbox.com', 'onedrive']
    if any(pattern in url_lower for pattern in cloud_patterns):
        return "direct"

    return "unknown"


# =============================================================================
# AZURE SEARCH SCHEMA
# =============================================================================

def debug_check_index_schema():
    if not SEARCH_ENDPOINT or not SEARCH_ADMIN_KEY or not SEARCH_INDEX_NAME:
        return "Search not configured"
    url = f"{SEARCH_ENDPOINT}/indexes/{SEARCH_INDEX_NAME}?api-version=2024-07-01"
    headers = {"api-key": SEARCH_ADMIN_KEY}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            schema = r.json()
            key_field = None
            fields_info = []
            for field in schema.get("fields", []):
                field_info = {
                    "name": field.get("name"),
                    "type": field.get("type"),
                    "key": field.get("key", False),
                    "retrievable": field.get("retrievable", False),
                    "filterable": field.get("filterable", False),
                    "sortable": field.get("sortable", False),
                    "facetable": field.get("facetable", False)
                }
                fields_info.append(field_info)
                if field.get("key", False):
                    key_field = field.get("name")
            return {
                "index_name": schema.get("name"),
                "key_field": key_field,
                "fields": fields_info,
            }
        else:
            return f"Index check failed: HTTP {r.status_code}"
    except Exception as e:
        return f"Error checking index: {str(e)}"


def get_index_schema():
    if st.session_state.get('index_schema_cache'):
        return st.session_state.index_schema_cache
    schema_info = debug_check_index_schema()
    if isinstance(schema_info, dict):
        st.session_state.index_schema_cache = schema_info
        return schema_info
    else:
        raise RuntimeError(f"Cannot fetch index schema: {schema_info}")


# =============================================================================
# VIDEO RETRIEVAL AND DELETION
# =============================================================================

def get_source_url_for_video(video_id: str) -> Optional[str]:
    if not SEARCH_ENDPOINT or not SEARCH_ADMIN_KEY or not SEARCH_INDEX_NAME:
        return None
    if not video_id or not isinstance(video_id, str):
        return None
    search_url = (
        f"{SEARCH_ENDPOINT}/indexes/{SEARCH_INDEX_NAME}"
        f"/docs/search?api-version=2024-07-01"
    )
    headers = {"api-key": SEARCH_ADMIN_KEY, "Content-Type": "application/json"}
    escaped_id = video_id.replace("'", "''")
    payload = {
        "search": "*",
        "filter": f"video_id eq '{escaped_id}'",
        "select": "video_id,source_url,source_type",
        "top": 1,
    }
    try:
        r = requests.post(search_url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        docs = r.json().get("value", [])
        if docs:
            source_url = docs[0].get("source_url")
            if source_url and isinstance(source_url, str):
                source_url = source_url.strip()
                if source_url:
                    return source_url
    except Exception as e:
        print(f"Error looking up source_url for {video_id}: {e}")
    return None


def get_stored_videos(
    video_id: str = None, source_type: str = None,
    include_missing: bool = True, limit: int = 1000
) -> List[Dict]:
    if not SEARCH_ENDPOINT or not SEARCH_ADMIN_KEY:
        return []
    url = (
        f"{SEARCH_ENDPOINT}/indexes/{SEARCH_INDEX_NAME}"
        f"/docs/search?api-version=2024-07-01"
    )
    headers = {"api-key": SEARCH_ADMIN_KEY, "Content-Type": "application/json"}
    try:
        schema = get_index_schema()
        available_fields = {f['name'] for f in schema.get('fields', [])}
    except Exception:
        available_fields = set()
    filters = []
    if video_id and isinstance(video_id, str) and video_id.strip():
        escaped_id = video_id.replace("'", "''")
        filters.append(f"video_id eq '{escaped_id}'")
    if source_type and isinstance(source_type, str) and source_type != "All":
        escaped_type = source_type.replace("'", "''")
        filters.append(f"source_type eq '{escaped_type}'")
    filter_query = " and ".join(filters) if filters else None
    select_fields = ["video_id"]
    for field in ["source_url", "source_type", "processed_at"]:
        if field in available_fields:
            select_fields.append(field)
    all_videos = {}
    skip = 0
    batch_size = 1000
    try:
        while True:
            payload = {
                "search": "*",
                "select": ",".join(select_fields),
                "top": batch_size,
                "skip": skip,
                "count": True,
            }
            if filter_query:
                payload["filter"] = filter_query
            if "processed_at" in available_fields:
                payload["orderby"] = "processed_at desc"
            r = requests.post(url, headers=headers, json=payload, timeout=30)
            r.raise_for_status()
            data = r.json()
            docs = data.get("value", [])
            if not docs:
                break
            for doc in docs:
                vid = doc.get('video_id')
                if vid and vid not in all_videos:
                    source_url = doc.get('source_url')
                    if source_url and isinstance(source_url, str):
                        source_url = source_url.strip()
                    else:
                        source_url = ''
                    all_videos[vid] = {
                        'video_id':     vid,
                        'source_type':  doc.get('source_type') or 'unknown',
                        'source_url':   source_url,
                        'processed_at': doc.get('processed_at', 'unknown'),
                    }
            skip += len(docs)
            if len(docs) < batch_size:
                break
        return list(all_videos.values())[:limit]
    except Exception as e:
        st.error(f"Failed to retrieve videos: {e}")
        return []


def delete_video_by_id(video_id: str) -> bool:
    if not SEARCH_ENDPOINT or not SEARCH_ADMIN_KEY:
        return False
    if not video_id or not isinstance(video_id, str):
        return False
    try:
        schema    = get_index_schema()
        key_field = schema.get('key_field', 'id')
    except Exception:
        key_field = 'id'
    search_url = (
        f"{SEARCH_ENDPOINT}/indexes/{SEARCH_INDEX_NAME}"
        f"/docs/search?api-version=2024-07-01"
    )
    headers    = {"api-key": SEARCH_ADMIN_KEY, "Content-Type": "application/json"}
    escaped_id = video_id.replace("'", "''")
    payload = {
        "search": "*",
        "filter": f"video_id eq '{escaped_id}'",
        "select": f"{key_field},video_id",
        "top": 1000,
    }
    try:
        r = requests.post(search_url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        docs = r.json().get("value", [])
        if not docs:
            return False
        delete_docs = []
        for doc in docs:
            doc_key = doc.get(key_field) or doc.get('id')
            if doc_key:
                delete_docs.append({"@search.action": "delete", key_field: doc_key})
        if not delete_docs:
            return False
        delete_url = (
            f"{SEARCH_ENDPOINT}/indexes/{SEARCH_INDEX_NAME}"
            f"/docs/index?api-version=2024-07-01"
        )
        r = requests.post(
            delete_url, headers=headers,
            json={"value": delete_docs}, timeout=60
        )
        r.raise_for_status()
        return True
    except Exception as e:
        st.error(f"Delete failed: {e}")
        return False


# =============================================================================
# PENDING UPLOAD TRACKING
#
# Written by the Upload page when a video is submitted, read (and cleared
# entry-by-entry) by the Manage Videos page when it checks on progress.
# This is what replaced the old "watch a progress bar" behavior.
# =============================================================================

def get_pending_uploads() -> Dict[str, Any]:
    """Read the pending-uploads tracking file. Returns {} if none exists yet."""
    if not AZURE_STORAGE_KEY:
        return {}
    try:
        service = BlobServiceClient(
            account_url=f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net",
            credential=AZURE_STORAGE_KEY,
        )
        bc = service.get_blob_client(container=PENDING_CONTAINER, blob="pending.json")
        return json.loads(bc.download_blob().readall())
    except Exception:
        return {}


def save_pending_uploads(pending: Dict[str, Any]) -> None:
    """Write the pending-uploads tracking file."""
    service = BlobServiceClient(
        account_url=f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net",
        credential=AZURE_STORAGE_KEY,
    )
    try:
        service.get_container_client(PENDING_CONTAINER).create_container()
    except Exception:
        pass
    bc = service.get_blob_client(container=PENDING_CONTAINER, blob="pending.json")
    bc.upload_blob(json.dumps(pending, ensure_ascii=False), overwrite=True)


# =============================================================================
# VIDEO ID GENERATION
# =============================================================================

def generate_video_id(filename: str) -> str:
    clean_name = Path(filename).stem
    clean_name = re.sub(r'[^\w\s-]', '', clean_name)
    clean_name = re.sub(r'[-\s]+', '_', clean_name)
    hash_suffix = hashlib.md5(clean_name.encode()).hexdigest()[:8]
    return f"vid_{clean_name[:50]}_{hash_suffix}"


# =============================================================================
# VIDEO LINK GENERATION
# =============================================================================

def get_box_audio_url(box_url: str) -> Tuple[Optional[str], bool]:
    """
    Convert a Box viewer URL to a direct audio URL suitable for fetching bytes.
    Returns (audio_url, is_embeddable).
    """
    from urllib.parse import urlparse, parse_qs

    if not box_url or "box.com" not in box_url.lower():
        return None, False

    try:
        parsed = urlparse(box_url.strip())
        qs     = parse_qs(parsed.query)
        base   = f"{parsed.scheme}://{parsed.netloc}"

        file_id_match = re.search(r'/file/(\d+)', parsed.path)
        shared_token  = qs.get('s', [None])[0]
        s_path_match  = re.match(r'/s/([^/?#]+)', parsed.path)

        if s_path_match:
            s_token = s_path_match.group(1)
            return f"{base}/s/{s_token}?dl=1", True

        if file_id_match and shared_token:
            file_id = file_id_match.group(1)
            url = (
                f"{base}/index.php"
                f"?rm=box_download_shared_file"
                f"&file_id=f_{file_id}"
                f"&shared_name={shared_token}"
            )
            return url, True

        if '/shared/static/' in box_url.lower():
            return box_url.strip(), True

    except Exception:
        pass

    return None, False


def fetch_box_audio_bytes(box_url: str) -> Optional[bytes]:
    """
    Fetch audio bytes from any Box shared URL for use in st.audio().

    Handles three URL patterns:
      - /s/{token}              shared links  → append ?dl=1
      - /file/{id}?s={token}   viewer links  → index.php then /content
      - /shared/static/{hash}  direct links  → use as-is
    Returns None if all attempts fail or return HTML.
    """
    from urllib.parse import urlparse, parse_qs

    if not box_url:
        return None

    try:
        parsed = urlparse(box_url.strip())
        qs     = parse_qs(parsed.query)
        base   = f"{parsed.scheme}://{parsed.netloc}"

        file_id_match = re.search(r'/file/(\d+)', parsed.path)
        shared_token  = qs.get('s', [None])[0]
        s_path_match  = re.match(r'/s/([^/?#]+)', parsed.path)

        candidates = []

        if s_path_match:
            s_token = s_path_match.group(1)
            candidates.append(f"{base}/s/{s_token}?dl=1")
            candidates.append(f"{base}/shared/static/{s_token}")

        elif file_id_match and shared_token:
            file_id = file_id_match.group(1)
            candidates.append(
                f"{base}/index.php"
                f"?rm=box_download_shared_file"
                f"&file_id=f_{file_id}"
                f"&shared_name={shared_token}"
            )
            candidates.append(f"{base}/file/{file_id}/content?s={shared_token}")

        elif '/shared/static/' in box_url.lower():
            candidates.append(box_url.strip())

        else:
            candidates.append(box_url.strip())

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }

        for url in candidates:
            try:
                resp = requests.get(
                    url, headers=headers,
                    timeout=60, allow_redirects=True
                )
                ct = resp.headers.get("Content-Type", "")
                if resp.status_code == 200 and "text/html" not in ct:
                    return resp.content
            except Exception:
                continue

    except Exception:
        pass

    return None


def build_video_link(
    video_id: str, start_ms: int,
    source_url: Optional[str] = None,
    source_type: Optional[str] = None
) -> Tuple[str, str, bool]:
    """
    Build a playable video link with time marker where supported.
    Returns (url, link_type_description, supports_time_marker).
    """
    start_sec = ms_to_seconds(start_ms)

    actual_source = None
    if source_url and isinstance(source_url, str):
        actual_source = source_url.strip() or None
    if not actual_source and video_id and isinstance(video_id, str):
        try:
            actual_source = get_source_url_for_video(video_id)
        except Exception:
            actual_source = None

    if not actual_source:
        return ("#", "No source URL stored", False)

    source_lower = actual_source.lower()

    # YouTube
    if "youtube.com" in source_lower or "youtu.be" in source_lower:
        base = re.sub(r'[?&](t|start)=\d+s?', '', actual_source)
        sep  = "&" if "?" in base else "?"
        return (f"{base}{sep}t={start_sec}s", "YouTube", True)

    # Box
    if "box.com" in source_lower or "boxcloud.com" in source_lower:
        if "/shared/static/" in source_lower:
            return (actual_source, "Box (download)", False)
        return (actual_source, "Box viewer", False)

    # Vimeo
    if "vimeo.com" in source_lower:
        base = actual_source.split("#")[0].split("?")[0]
        return (f"{base}#t={start_sec}s", "Vimeo", True)

    # Internal SAS / blob storage
    if (
        "blob.core.windows.net" in source_lower
        or "sig=" in actual_source
        or actual_source.startswith("uploaded_file://")
    ):
        return ("#", "Internal storage (no public link)", False)

    # Generic direct URL
    return (actual_source, "Direct", False)
