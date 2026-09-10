"""
EmbedAndIndex - Azure Function for Embedding Generation and Search Indexing

This Azure Function processes video segments for search:
1. Reads segment JSON from Azure Blob Storage
2. Generates vector embeddings via Azure OpenAI for each segment's text
3. Indexes segments with embeddings into Azure AI Search

Architecture Role:
- Part of the ingestion pipeline (called by import_videos.py after transcription)
- Prepares segments for searchability by creating embeddings and indexing
- Enables hybrid search (keyword + vector) in SearchSegments function

Input: POST with segments_blob path (e.g., "segments/vid_xxx.json")

Output: JSON with video_id and count of indexed segments

Note: Processes embeddings in batches (16 segments) and indexes in batches (500 docs)
"""

import json
import logging
import os
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List

import azure.functions as func
import requests
from azure.storage.blob import BlobServiceClient


SEARCH_API_VERSION = "2024-05-01-preview"  # recommended for vector index creation/query patterns
EMBED_BATCH_SIZE = 16
INDEX_BATCH_SIZE = 500


def _blob_service() -> BlobServiceClient:
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    key = os.environ["AZURE_STORAGE_KEY"]
    return BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=key,
    )


def _read_json_blob(container: str, blob_name: str) -> Dict[str, Any]:
    service = _blob_service()
    bc = service.get_blob_client(container=container, blob=blob_name)
    data = bc.download_blob().readall()
    return json.loads(data)


def _embed_texts(texts: List[str]) -> List[List[float]]:
    """
    Calls Azure OpenAI embeddings:
    POST {endpoint}/openai/deployments/{deployment}/embeddings?api-version=...
    """
    endpoint = os.environ["EMBEDDINGS_ENDPOINT"].rstrip("/")
    key = os.environ["EMBEDDINGS_KEY"]
    deployment = os.environ["EMBEDDINGS_DEPLOYMENT"]
    api_version = os.environ.get("EMBEDDINGS_API_VERSION", "2024-10-21")

    url = f"{endpoint}/openai/deployments/{deployment}/embeddings?api-version={api_version}"
    headers = {
        "Content-Type": "application/json",
        "api-key": key,
    }
    payload = {"input": texts}

    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Embeddings failed: {r.status_code} {r.text}")

    data = r.json()
    # data["data"] is list aligned to inputs (by index)
    # each item has "embedding"
    out = [item["embedding"] for item in sorted(data["data"], key=lambda x: x["index"])]
    return out


def _search_index_documents(docs: List[Dict[str, Any]]) -> None:
    endpoint = os.environ["SEARCH_ENDPOINT"].rstrip("/")
    key = os.environ["SEARCH_ADMIN_KEY"]
    index_name = os.environ.get("SEARCH_INDEX", "segments")

    url = f"{endpoint}/indexes/{index_name}/docs/index?api-version={SEARCH_API_VERSION}"
    headers = {
        "Content-Type": "application/json",
        "api-key": key,
    }
    payload = {"value": docs}

    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Search indexing failed: {r.status_code} {r.text}")

    resp = r.json()
    # If partial failures, surface them
    failed = [v for v in resp.get("value", []) if not v.get("succeeded", True)]
    if failed:
        raise RuntimeError(f"Search indexing had failures: {failed[:3]} (showing first 3)")


def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
        segments_blob = body.get("segments_blob")
        if not segments_blob or "/" not in segments_blob:
            return func.HttpResponse(
                json.dumps({"error": "Provide 'segments_blob' like 'segments/<video_id>.json'."}),
                mimetype="application/json",
                status_code=400,
            )

        container, blob_name = segments_blob.split("/", 1)
        payload = _read_json_blob(container, blob_name)

        video_id = payload.get("video_id")
        segments = payload.get("segments") or []
        if not video_id or not isinstance(segments, list):
            return func.HttpResponse(
                json.dumps({"error": "Invalid segments payload. Expected {video_id, segments:[...]}."}),
                mimetype="application/json",
                status_code=400,
            )

        source_url = body.get("source_url") or ""
        source_type = body.get("source_type") or "unknown"
        processed_at = datetime.now(timezone.utc).isoformat()

        # Prepare texts to embed
        # (Skip empty text segments to save money; still index them with empty embedding if you prefer.)
        nonempty = [s for s in segments if (s.get("text") or "").strip()]

        # Embed in batches
        embeddings_map: Dict[str, List[float]] = {}
        for i in range(0, len(nonempty), EMBED_BATCH_SIZE):
            batch = nonempty[i : i + EMBED_BATCH_SIZE]
            texts = [(s.get("text") or "") for s in batch]
            embs = _embed_texts(texts)
            for s, e in zip(batch, embs):
                seg_id = s["segment_id"]
                embeddings_map[seg_id] = e

        # Build search documents
        # Use @search.action=mergeOrUpload so reruns overwrite.
        docs_to_send: List[Dict[str, Any]] = []
        for s in segments:
            seg_id = s["segment_id"]
            doc = {
                "@search.action": "mergeOrUpload",
                "segment_key": f"{video_id}_{seg_id}",
                "video_id": video_id,
                "segment_id": seg_id,
                "source_url": source_url,
                "source_type": source_type,
                "processed_at": processed_at,
                "start_ms": int(s.get("start_ms", 0)),
                "end_ms": int(s.get("end_ms", 0)),
                "text": (s.get("text") or "").strip(),
                # vector field (only include if we have it)
            }
            if seg_id in embeddings_map:
                doc["embedding"] = embeddings_map[seg_id]

            docs_to_send.append(doc)

        # Index in batches
        for i in range(0, len(docs_to_send), INDEX_BATCH_SIZE):
            _search_index_documents(docs_to_send[i : i + INDEX_BATCH_SIZE])

        return func.HttpResponse(
            json.dumps({"video_id": video_id, "indexed": len(docs_to_send)}),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        logging.exception("EmbedAndIndex failed")
        return func.HttpResponse(
            json.dumps({"error": str(e), "trace": traceback.format_exc()}),
            mimetype="application/json",
            status_code=500,
        )
    # except Exception as e:
    #     return func.HttpResponse(
    #         json.dumps({"error": str(e)}),
    #         mimetype="application/json",
    #         status_code=500,
    #     )
