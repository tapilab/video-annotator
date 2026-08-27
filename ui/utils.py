"""
utils.py - Shared utilities for Video Annotation Platform
"""

import os
import requests
import streamlit as st
import json
import time
import re
import subprocess
import hashlib
import base64
import hmac
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Tuple, List
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

__all__ = [
    "SEARCH_FN_URL", "SPEECH_KEY", "SPEECH_REGION", "SPEECH_API_VERSION",
    "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_KEY", "AZURE_OPENAI_DEPLOYMENT",
    "SEARCH_ENDPOINT", "SEARCH_KEY", "SEARCH_INDEX_NAME",
    "AZURE_STORAGE_ACCOUNT", "AZURE_STORAGE_KEY", "INPUT_CONTAINER", "SEGMENTS_CONTAINER",
    "POLL_SECONDS",
    "ms_to_ts", "ms_to_seconds", "sanitize_id", "detect_url_type", "check_yt_dlp",
    "debug_check_index_schema", "get_index_schema",
    "submit_transcription_direct", "poll_transcription_operation", "get_transcription_from_result",
    "get_embeddings", "index_segments_direct", "process_transcription_to_segments",
    "get_stored_videos", "delete_video_by_id", "get_source_url_for_video",
    "generate_video_id", "test_sas_url", "generate_sas_token_fixed",
    "upload_to_azure_blob_fixed", "upload_to_azure_blob_sdk", "save_segments_to_blob",
    "download_youtube_audio", "download_box_audio", "get_box_audio_url",
    "fetch_box_audio_bytes", "process_single_video", "build_video_link",
]

# =============================================================================
# CONFIGURATION
# =============================================================================
SEARCH_FN_URL = os.environ.get("SEARCH_FN_URL", "")

SPEECH_KEY = os.environ.get("SPEECH_KEY")
SPEECH_REGION = os.environ.get("SPEECH_REGION", "eastus")
SPEECH_API_VERSION = os.environ.get("SPEECH_API_VERSION", "2024-11-15")

AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_KEY = os.environ.get("AZURE_OPENAI_KEY")
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "text-embedding-3-small")

SEARCH_ENDPOINT = os.environ.get("SEARCH_ENDPOINT")
SEARCH_KEY = os.environ.get("SEARCH_KEY")
SEARCH_INDEX_NAME = os.environ.get("SEARCH_INDEX_NAME", "segments")

AZURE_STORAGE_ACCOUNT = os.environ.get("AZURE_STORAGE_ACCOUNT", "storagevideoannotator")
AZURE_STORAGE_KEY = os.environ.get("AZURE_STORAGE_KEY", "")
INPUT_CONTAINER = os.environ.get("INPUT_CONTAINER", "speech-input")
SEGMENTS_CONTAINER = os.environ.get("SEGMENTS_CONTAINER", "segments")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "15"))

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


def sanitize_id(id_string: str) -> str:
    if not id_string:
        id_string = "unknown"
    sanitized = re.sub(r'[^\w\-]', '_', str(id_string))
    sanitized = re.sub(r'_+', '_', sanitized)
    sanitized = sanitized.strip('_')
    if not sanitized:
        sanitized = "unknown"
    if sanitized.startswith('_') or sanitized.startswith('-'):
        sanitized = 'id' + sanitized
    if len(sanitized) > 1024:
        hash_suffix = hashlib.md5(sanitized.encode()).hexdigest()[:16]
        sanitized = sanitized[:1000] + "_" + hash_suffix
    return sanitized


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


def check_yt_dlp() -> bool:
    try:
        result = subprocess.run(["which", "yt-dlp"], capture_output=True, text=True)
        return result.returncode == 0
    except Exception:
        return False


# =============================================================================
# AZURE SEARCH SCHEMA FUNCTIONS
# =============================================================================

def debug_check_index_schema():
    if not SEARCH_ENDPOINT or not SEARCH_KEY or not SEARCH_INDEX_NAME:
        return "Search not configured"
    url = f"{SEARCH_ENDPOINT}/indexes/{SEARCH_INDEX_NAME}?api-version=2024-07-01"
    headers = {"api-key": SEARCH_KEY}
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
# AZURE SPEECH API FUNCTIONS
# =============================================================================

def submit_transcription_direct(video_id: str, media_url: str) -> Dict[str, Any]:
    if not SPEECH_KEY:
        raise RuntimeError("SPEECH_KEY not configured")
    endpoint = (
        f"https://{SPEECH_REGION}.api.cognitive.microsoft.com"
        f"/speechtotext/transcriptions:submit?api-version={SPEECH_API_VERSION}"
    )
    headers = {
        "Ocp-Apim-Subscription-Key": SPEECH_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "contentUrls": [media_url],
        "locale": "en-US",
        "displayName": f"transcription_{video_id}",
        "properties": {
            "diarizationEnabled": False,
            "wordLevelTimestampsEnabled": False,
            "punctuationMode": "DictatedAndAutomatic",
            "profanityFilterMode": "Masked",
            "timeToLiveHours": 24
        }
    }
    try:
        r = requests.post(endpoint, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        operation_url = r.headers.get("Location")
        if not operation_url:
            result = r.json()
            operation_url = result.get("self") or result.get("links", {}).get("self")
        if not operation_url:
            raise RuntimeError("No operation URL returned")
        return {"operation_url": operation_url, "video_id": video_id}
    except requests.exceptions.HTTPError:
        error_msg = f"Speech API error {r.status_code}: {r.text}"
        if r.status_code == 401:
            error_msg = "Azure Speech API authentication failed. Check SPEECH_KEY."
        raise RuntimeError(error_msg)


def poll_transcription_operation(operation_url: str) -> Dict[str, Any]:
    if not SPEECH_KEY:
        raise RuntimeError("SPEECH_KEY not configured")
    headers = {"Ocp-Apim-Subscription-Key": SPEECH_KEY}
    try:
        poll_url = operation_url.replace("/transcriptions:submit/", "/transcriptions/")
        st.session_state['debug_poll_url'] = poll_url
        r = requests.get(poll_url, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to poll transcription: {str(e)}")


def get_transcription_from_result(result_data: Dict) -> Dict[str, Any]:
    if not SPEECH_KEY:
        raise RuntimeError("SPEECH_KEY not configured")
    headers = {"Ocp-Apim-Subscription-Key": SPEECH_KEY}
    try:
        links = result_data.get("links", {})
        files_url = links.get("files")
        if not files_url:
            if "combinedRecognizedPhrases" in result_data:
                return result_data
            raise RuntimeError("No files URL in result")
        r = requests.get(files_url, headers=headers, timeout=30)
        r.raise_for_status()
        files_data = r.json()
        for file in files_data.get("values", []):
            if file.get("kind") == "Transcription":
                content_url = file.get("links", {}).get("contentUrl")
                if content_url:
                    content_r = requests.get(content_url, timeout=60)
                    content_r.raise_for_status()
                    return content_r.json()
        raise RuntimeError("No transcription file found")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to get transcription result: {str(e)}")


# =============================================================================
# EMBEDDING AND INDEXING
# =============================================================================

def get_embeddings(texts: list) -> list:
    if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_KEY:
        raise RuntimeError("Azure OpenAI not configured")
    url = (
        f"{AZURE_OPENAI_ENDPOINT}/openai/deployments/{AZURE_OPENAI_DEPLOYMENT}"
        f"/embeddings?api-version=2024-02-01"
    )
    headers = {"api-key": AZURE_OPENAI_KEY, "Content-Type": "application/json"}
    payload = {"input": texts, "model": "text-embedding-3-small"}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        result = r.json()
        return [item["embedding"] for item in result["data"]]
    except Exception as e:
        raise RuntimeError(f"Embedding failed: {str(e)}")


def index_segments_direct(
    video_id: str, segments: list,
    source_url: str = None, source_type: str = None
) -> Dict[str, Any]:
    if not SEARCH_ENDPOINT or not SEARCH_KEY:
        raise RuntimeError("Azure Search not configured")
    schema_info = get_index_schema()
    key_field = schema_info.get("key_field")
    available_fields = {f.get("name") for f in schema_info.get("fields", [])}
    if not key_field:
        raise RuntimeError("No key field found")
    url_fields_available = {
        'source_url':   'source_url'   in available_fields,
        'source_type':  'source_type'  in available_fields,
        'processed_at': 'processed_at' in available_fields,
    }
    texts = [seg.get("text", "") for seg in segments]
    try:
        embeddings = get_embeddings(texts)
    except Exception as e:
        st.warning(f"Embedding failed, indexing without vectors: {e}")
        embeddings = [None] * len(segments)
    documents = []
    processed_timestamp = datetime.utcnow().isoformat() + "Z"
    for i, (seg, embedding) in enumerate(zip(segments, embeddings)):
        safe_video_id = sanitize_id(video_id)
        doc_id = f"{safe_video_id}_{i}"
        doc = {"@search.action": "upload", key_field: doc_id}
        field_mappings = {
            "video_id":   safe_video_id,
            "segment_id": str(seg.get("segment_id", i)),
            "text":       str(seg.get("text", "")),
            "start_ms":   int(seg.get("start_ms", 0)),
            "end_ms":     int(seg.get("end_ms", 0)),
            "pred_labels": seg.get("pred_labels", []) if seg.get("pred_labels") else [],
        }
        if url_fields_available['source_url']:
            field_mappings["source_url"] = str(source_url) if source_url else ""
        if url_fields_available['source_type']:
            field_mappings["source_type"] = str(source_type) if source_type else "unknown"
        if url_fields_available['processed_at']:
            field_mappings["processed_at"] = processed_timestamp
        for field_name, value in field_mappings.items():
            if field_name in available_fields:
                doc[field_name] = value
        embedding_field = next(
            (f for f in ["embedding", "embeddings", "vector", "vectors"]
             if f in available_fields),
            None
        )
        if embedding and embedding_field:
            try:
                doc[embedding_field] = [float(x) for x in embedding]
            except (ValueError, TypeError):
                pass
        documents.append(doc)
    url = f"{SEARCH_ENDPOINT}/indexes/{SEARCH_INDEX_NAME}/docs/index?api-version=2024-07-01"
    headers = {"api-key": SEARCH_KEY, "Content-Type": "application/json"}
    payload = {"value": documents}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"Indexing failed: HTTP {r.status_code}")
        return {
            "indexed": len(documents),
            "video_id": video_id,
            "key_field_used": key_field,
            "source_url_stored":  bool(source_url  and url_fields_available['source_url']),
            "source_type_stored": bool(source_type and url_fields_available['source_type']),
            "url_fields_available": url_fields_available,
        }
    except Exception as e:
        raise RuntimeError(f"Indexing failed: {str(e)}")


def process_transcription_to_segments(
    transcription_data: Dict, video_id: str, window_ms: int = 30000
) -> list:
    """
    Group recognized phrases into ~30s chunks for more substantive segments.
    window_ms controls chunk size: 30000=30s, 45000=45s, 60000=1min.
    Tail segments shorter than half the window are merged into the previous segment.
    """
    phrases = []
    for phrase in transcription_data.get("recognizedPhrases", []):
        offset   = phrase.get("offsetInTicks", 0) // 10000
        duration = phrase.get("durationInTicks", 0) // 10000
        nbest    = phrase.get("nBest", [])
        text     = nbest[0].get("display", "") if nbest else ""
        if text.strip():
            phrases.append({
                "text":     text,
                "start_ms": offset,
                "end_ms":   offset + duration,
            })

    if not phrases:
        return []

    segments      = []
    current_texts = []
    current_start = phrases[0]["start_ms"]
    current_end   = phrases[0]["end_ms"]

    for phrase in phrases:
        if phrase["start_ms"] - current_start >= window_ms and current_texts:
            segments.append({
                "segment_id":  len(segments),
                "video_id":    video_id,
                "text":        " ".join(current_texts),
                "start_ms":    current_start,
                "end_ms":      current_end,
                "pred_labels": [],
            })
            current_texts = []
            current_start = phrase["start_ms"]

        current_texts.append(phrase["text"])
        current_end = phrase["end_ms"]

    # Flush final chunk — merge into previous if shorter than half the window
    if current_texts:
        tail_duration = current_end - current_start
        if segments and tail_duration < window_ms // 2:
            last = segments[-1]
            last["text"]   = last["text"] + " " + " ".join(current_texts)
            last["end_ms"] = current_end
        else:
            segments.append({
                "segment_id":  len(segments),
                "video_id":    video_id,
                "text":        " ".join(current_texts),
                "start_ms":    current_start,
                "end_ms":      current_end,
                "pred_labels": [],
            })

    return segments


# =============================================================================
# VIDEO RETRIEVAL AND DELETION
# =============================================================================

def get_source_url_for_video(video_id: str) -> Optional[str]:
    if not SEARCH_ENDPOINT or not SEARCH_KEY or not SEARCH_INDEX_NAME:
        return None
    if not video_id or not isinstance(video_id, str):
        return None
    search_url = (
        f"{SEARCH_ENDPOINT}/indexes/{SEARCH_INDEX_NAME}"
        f"/docs/search?api-version=2024-07-01"
    )
    headers = {"api-key": SEARCH_KEY, "Content-Type": "application/json"}
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
    if not SEARCH_ENDPOINT or not SEARCH_KEY:
        return []
    url = (
        f"{SEARCH_ENDPOINT}/indexes/{SEARCH_INDEX_NAME}"
        f"/docs/search?api-version=2024-07-01"
    )
    headers = {"api-key": SEARCH_KEY, "Content-Type": "application/json"}
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
    if not SEARCH_ENDPOINT or not SEARCH_KEY:
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
    headers    = {"api-key": SEARCH_KEY, "Content-Type": "application/json"}
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
# AZURE STORAGE FUNCTIONS
# =============================================================================

def generate_video_id(filename: str) -> str:
    clean_name = Path(filename).stem
    clean_name = re.sub(r'[^\w\s-]', '', clean_name)
    clean_name = re.sub(r'[-\s]+', '_', clean_name)
    hash_suffix = hashlib.md5(clean_name.encode()).hexdigest()[:8]
    return f"vid_{clean_name[:50]}_{hash_suffix}"


def test_sas_url(sas_url: str) -> Tuple[bool, str]:
    try:
        r = requests.head(sas_url, timeout=10, allow_redirects=True)
        return (r.status_code == 200, f"HTTP {r.status_code}")
    except Exception as e:
        return (False, str(e))


def generate_sas_token_fixed(blob_name: str, expiry_hours: int = 24) -> Optional[str]:
    if not AZURE_STORAGE_KEY:
        return None
    try:
        expiry     = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)
        expiry_str = expiry.strftime('%Y-%m-%dT%H:%M:%SZ')
        account_key = base64.b64decode(AZURE_STORAGE_KEY)
        canonicalized_resource = (
            f"/blob/{AZURE_STORAGE_ACCOUNT}/{INPUT_CONTAINER}/{blob_name}"
        )
        string_to_sign = (
            f"r\n\n{expiry_str}\n{canonicalized_resource}\n\n\n"
            f"https\n2020-12-06\nb\n\n\n\n\n\n\n"
        )
        signed_hmac = hmac.new(
            account_key, string_to_sign.encode('utf-8'), hashlib.sha256
        ).digest()
        signature = base64.b64encode(signed_hmac).decode('utf-8')
        sas_params = {
            'sv': '2020-12-06', 'sr': 'b', 'sp': 'r',
            'se': expiry_str, 'spr': 'https', 'sig': signature,
        }
        return '&'.join(
            [f"{k}={urllib.parse.quote(v, safe='')}" for k, v in sas_params.items()]
        )
    except Exception as e:
        st.error(f"SAS generation error: {e}")
        return None


def upload_to_azure_blob_fixed(
    file_bytes: bytes, blob_name: str
) -> Tuple[Optional[str], Optional[str]]:
    if not AZURE_STORAGE_KEY:
        return None, "Azure Storage key not configured"
    try:
        url = (
            f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net"
            f"/{INPUT_CONTAINER}/{blob_name}"
        )
        date_str       = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
        content_length = len(file_bytes)
        string_to_sign = (
            f"PUT\n\n\n{content_length}\n\napplication/octet-stream\n\n\n\n\n\n\n"
            f"x-ms-blob-type:BlockBlob\nx-ms-date:{date_str}\nx-ms-version:2020-12-06\n"
            f"/{AZURE_STORAGE_ACCOUNT}/{INPUT_CONTAINER}/{blob_name}"
        )
        account_key  = base64.b64decode(AZURE_STORAGE_KEY)
        signed_hmac  = hmac.new(
            account_key, string_to_sign.encode('utf-8'), hashlib.sha256
        ).digest()
        signature = base64.b64encode(signed_hmac).decode('utf-8')
        headers = {
            "x-ms-date":       date_str,
            "x-ms-version":    "2020-12-06",
            "x-ms-blob-type":  "BlockBlob",
            "Content-Type":    "application/octet-stream",
            "Content-Length":  str(content_length),
            "Authorization":   f"SharedKey {AZURE_STORAGE_ACCOUNT}:{signature}",
        }
        r = requests.put(url, data=file_bytes, headers=headers, timeout=300)
        if r.status_code not in [201, 200]:
            return None, f"Upload failed: HTTP {r.status_code}"
        sas_token = generate_sas_token_fixed(blob_name)
        if not sas_token:
            return None, "Failed to generate SAS token"
        sas_url = f"{url}?{sas_token}"
        is_valid, test_msg = test_sas_url(sas_url)
        if not is_valid:
            return None, f"SAS URL validation failed: {test_msg}"
        return sas_url, None
    except Exception as e:
        return None, f"Upload error: {str(e)}"


def upload_to_azure_blob_sdk(
    file_bytes: bytes, blob_name: str
) -> Tuple[Optional[str], Optional[str]]:
    try:
        from azure.storage.blob import (
            BlobServiceClient, generate_blob_sas, BlobSasPermissions
        )
        connection_string = (
            f"DefaultEndpointsProtocol=https;"
            f"AccountName={AZURE_STORAGE_ACCOUNT};"
            f"AccountKey={AZURE_STORAGE_KEY};"
            f"EndpointSuffix=core.windows.net"
        )
        blob_service     = BlobServiceClient.from_connection_string(connection_string)
        container_client = blob_service.get_container_client(INPUT_CONTAINER)
        try:
            container_client.create_container()
        except Exception:
            pass
        blob_client = container_client.get_blob_client(blob_name)
        blob_client.upload_blob(file_bytes, overwrite=True)
        sas_token = generate_blob_sas(
            account_name=AZURE_STORAGE_ACCOUNT,
            container_name=INPUT_CONTAINER,
            blob_name=blob_name,
            account_key=AZURE_STORAGE_KEY,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=24),
            protocol="https",
        )
        sas_url = (
            f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net"
            f"/{INPUT_CONTAINER}/{blob_name}?{sas_token}"
        )
        is_valid, test_msg = test_sas_url(sas_url)
        if not is_valid:
            return None, f"SAS URL validation failed: {test_msg}"
        return sas_url, None
    except ImportError:
        return None, "azure-storage-blob not installed"
    except Exception as e:
        return None, f"SDK upload failed: {str(e)}"


def save_segments_to_blob(video_id: str, segments: list) -> str:
    if not AZURE_STORAGE_KEY:
        raise RuntimeError("Azure Storage key not configured")
    blob_name = f"{video_id}_segments.json"
    url = (
        f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net"
        f"/{SEGMENTS_CONTAINER}/{blob_name}"
    )
    json_bytes     = json.dumps(segments, indent=2).encode('utf-8')
    content_length = len(json_bytes)
    date_str       = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
    string_to_sign = (
        f"PUT\n\n\n{content_length}\n\napplication/json\n\n\n\n\n\n\n"
        f"x-ms-blob-type:BlockBlob\nx-ms-date:{date_str}\nx-ms-version:2020-12-06\n"
        f"/{AZURE_STORAGE_ACCOUNT}/{SEGMENTS_CONTAINER}/{blob_name}"
    )
    account_key = base64.b64decode(AZURE_STORAGE_KEY)
    signed_hmac  = hmac.new(
        account_key, string_to_sign.encode('utf-8'), hashlib.sha256
    ).digest()
    signature = base64.b64encode(signed_hmac).decode('utf-8')
    headers = {
        "x-ms-date":      date_str,
        "x-ms-version":   "2020-12-06",
        "x-ms-blob-type": "BlockBlob",
        "Content-Type":   "application/json",
        "Content-Length": str(content_length),
        "Authorization":  f"SharedKey {AZURE_STORAGE_ACCOUNT}:{signature}",
    }
    r = requests.put(url, data=json_bytes, headers=headers, timeout=60)
    r.raise_for_status()
    return blob_name


# =============================================================================
# DOWNLOAD FUNCTIONS
# =============================================================================

def download_youtube_audio(
    youtube_url: str, output_path: str,
    progress_callback=None
) -> Tuple[Optional[str], Optional[str]]:
    if not check_yt_dlp():
        return None, "yt-dlp not installed"
    if not youtube_url or not youtube_url.strip():
        return None, "YouTube URL is empty"
    try:
        cmd = [
            "yt-dlp",
            "-f", "bestaudio[ext=m4a]/bestaudio",
            "--extract-audio",
            "--audio-format", "m4a",
            "--audio-quality", "0",
            "--no-check-certificate",
            "--no-warnings",
            "-o", output_path,
            youtube_url.strip(),
        ]
        try:
            node_check = subprocess.run(["which", "node"], capture_output=True, text=True)
            if node_check.returncode != 0:
                cmd.extend(["--extractor-args", "youtube:player_client=web"])
        except Exception:
            pass
        if progress_callback:
            progress_callback(15, "Downloading from YouTube...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            error_msg = result.stderr[:500]
            if "JavaScript runtime" in error_msg:
                error_msg += "\n\n💡 Tip: Install Node.js or run: pip install yt-dlp --upgrade"
            return None, f"yt-dlp failed: {error_msg}"
        if os.path.exists(output_path):
            return output_path, None
        base = output_path.rsplit('.', 1)[0]
        for ext in ['.m4a', '.mp3', '.webm', '.opus']:
            alt_path = base + ext
            if os.path.exists(alt_path):
                return alt_path, None
        return None, "Download completed but file not found"
    except subprocess.TimeoutExpired:
        return None, "Download timed out after 10 minutes"
    except Exception as e:
        return None, f"Error: {str(e)}"


def download_box_audio(
    box_url: str, output_path: str,
    progress_callback=None
) -> Tuple[Optional[str], Optional[str]]:
    """
    Download audio from a Box shared file URL.

    Handles three URL patterns:
      - /s/{token}              shared links  → append ?dl=1
      - /file/{id}?s={token}   viewer links  → /content or index.php endpoints
      - /shared/static/{hash}  direct links  → use as-is
    """
    from urllib.parse import urlparse, parse_qs

    if not box_url or not box_url.strip():
        return None, "Box URL is empty"

    try:
        parsed = urlparse(box_url.strip())
        qs     = parse_qs(parsed.query)
        base   = f"{parsed.scheme}://{parsed.netloc}"
        url_lower = box_url.lower()

        file_id_match = re.search(r'/file/(\d+)', parsed.path)
        shared_token  = qs.get('s', [None])[0]
        s_path_match  = re.match(r'/s/([^/?#]+)', parsed.path)

        candidates = []

        if s_path_match:
            # /s/{token} — standard Box shared link
            s_token = s_path_match.group(1)
            candidates.append(f"{base}/s/{s_token}?dl=1")
            candidates.append(f"{base}/shared/static/{s_token}")

        elif file_id_match and shared_token:
            # /file/{id}?s={token} viewer links
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

        if progress_callback:
            progress_callback(15, "Downloading from Box...")

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
                    cd_match = re.search(r'filename=["\']?([^"\';\s]+)', disposition)
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
            f"Could not download the Box file automatically ({last_error}).\n\n"
            "To get a direct download URL:\n"
            "1. Open the Box link in your browser\n"
            "2. Click the Download button (↓ icon in the top toolbar)\n"
            "3. The resulting URL will look like:\n"
            "   https://tulane.app.box.com/shared/static/<hash>.m4a\n"
            "4. Paste that URL into the Media URL field instead."
        )

    except Exception as e:
        return None, f"Box download error: {str(e)}"


# =============================================================================
# MAIN VIDEO PROCESSING
# =============================================================================

def process_single_video(
    url: str, custom_id: Optional[str] = None,
    source_type: str = "unknown",
    progress_bar=None, status_text=None,
    overall_progress: Tuple[int, int] = (0, 1)
) -> Dict[str, Any]:
    result = {
        "url":            url,
        "video_id":       None,
        "status":         "pending",
        "segments_count": 0,
        "error":          None,
        "index_status":   None,
        "source_url":     url,
        "source_type":    source_type,
        "url_stored":     False,
    }
    try:
        url_type = detect_url_type(url)
        if url_type == "unknown":
            result["status"] = "failed"
            result["error"]  = "Unknown URL type"
            return result

        video_id = custom_id.strip() if custom_id else generate_video_id(f"batch_{url}")
        result["video_id"] = video_id
        current, total = overall_progress
        base_progress = int((current / total) * 100) if progress_bar else 0

        if status_text:
            status_text.text(f"[{current}/{total}] Processing: {video_id}")

        media_url = None

        if url_type == "youtube":
            if not check_yt_dlp():
                result["status"] = "failed"
                result["error"]  = "yt-dlp not installed"
                return result
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                if status_text:
                    status_text.text(f"[{current}/{total}] Downloading from YouTube...")
                output_path     = f"{tmpdir}/youtube_{video_id}.m4a"
                downloaded_path, error = download_youtube_audio(url.strip(), output_path)
                if error:
                    result["status"] = "failed"
                    result["error"]  = f"Download failed: {error}"
                    return result
                with open(downloaded_path, 'rb') as f:
                    file_bytes = f.read()
                blob_name = f"batch_youtube_{video_id}_{int(time.time())}.m4a"
                if status_text:
                    status_text.text(f"[{current}/{total}] Uploading to Azure...")
                sas_url, error = upload_to_azure_blob_sdk(file_bytes, blob_name)
                if error and ("not installed" in error or "SDK" in error):
                    sas_url, error = upload_to_azure_blob_fixed(file_bytes, blob_name)
                if error:
                    result["status"] = "failed"
                    result["error"]  = f"Upload failed: {error}"
                    return result
                media_url = sas_url

        elif url_type == "box":
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                if status_text:
                    status_text.text(f"[{current}/{total}] Downloading from Box...")
                output_path     = f"{tmpdir}/box_{video_id}.m4a"
                downloaded_path, error = download_box_audio(url.strip(), output_path)
                if error:
                    result["status"] = "failed"
                    result["error"]  = f"Box download failed: {error}"
                    return result
                with open(downloaded_path, 'rb') as f:
                    file_bytes = f.read()
                blob_name = f"batch_box_{video_id}_{int(time.time())}.m4a"
                if status_text:
                    status_text.text(f"[{current}/{total}] Uploading to Azure...")
                sas_url, error = upload_to_azure_blob_sdk(file_bytes, blob_name)
                if error and ("not installed" in error or "SDK" in error):
                    sas_url, error = upload_to_azure_blob_fixed(file_bytes, blob_name)
                if error:
                    result["status"] = "failed"
                    result["error"]  = f"Upload failed: {error}"
                    return result
                media_url = sas_url

        elif url_type == "direct":
            media_url = url.strip()
            if status_text:
                status_text.text(f"[{current}/{total}] Using direct URL...")

        if not media_url:
            result["status"] = "failed"
            result["error"]  = "No media URL available"
            return result

        if status_text:
            status_text.text(f"[{current}/{total}] Submitting to Speech API...")
        submit_result = submit_transcription_direct(video_id, media_url)
        operation_url = submit_result.get("operation_url")
        if not operation_url:
            result["status"] = "failed"
            result["error"]  = "No operation URL returned"
            return result

        max_polls         = 120
        transcription_data = None
        for i in range(max_polls):
            time.sleep(POLL_SECONDS)
            poll_result = poll_transcription_operation(operation_url)
            status      = poll_result.get("status", "unknown")
            if progress_bar:
                poll_progress = min(int((i / max_polls) * 20), 20)
                overall = (
                    base_progress
                    + int((1 / total) * 80)
                    + int((1 / total) * poll_progress)
                )
                progress_bar.progress(min(overall, 99))
            if status.lower() == "succeeded":
                transcription_data = get_transcription_from_result(poll_result)
                break
            elif status.lower() == "failed":
                error_msg = (
                    poll_result.get("properties", {})
                    .get("error", {})
                    .get("message", "Unknown error")
                )
                result["status"] = "failed"
                result["error"]  = f"Transcription failed: {error_msg}"
                return result

        if not transcription_data:
            result["status"] = "failed"
            result["error"]  = "Transcription timed out"
            return result

        if status_text:
            status_text.text(f"[{current}/{total}] Processing segments...")
        segments = process_transcription_to_segments(transcription_data, video_id)
        result["segments_count"] = len(segments)
        save_segments_to_blob(video_id, segments)

        try:
            index_result = index_segments_direct(
                video_id, segments,
                source_url=url,
                source_type=source_type,
            )
            result["url_stored"]    = index_result.get('source_url_stored', False)
            result["index_status"]  = f"Indexed {index_result.get('indexed', 0)} documents"
            st.session_state['debug_info'][video_id] = {
                'url_fields_available': index_result.get('url_fields_available', {}),
                'source_url_stored':    index_result.get('source_url_stored', False),
                'source_type_stored':   index_result.get('source_type_stored', False),
            }
        except Exception as e:
            result["index_status"] = f"Indexing failed: {str(e)}"

        result["status"] = "success"

    except Exception as e:
        result["status"] = "failed"
        result["error"]  = str(e)
        import traceback
        result["error"] += f"\n{traceback.format_exc()}"

    return result


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
            # /s/{token} — standard Box shared link: ?dl=1 forces download
            s_token = s_path_match.group(1)
            candidates.append(f"{base}/s/{s_token}?dl=1")
            candidates.append(f"{base}/shared/static/{s_token}")

        elif file_id_match and shared_token:
            # /file/{id}?s={token} viewer links
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