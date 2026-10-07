"""
SearchSegments - Azure Function for Searching Indexed Video Segments

This Azure Function provides search capabilities over indexed video segments:
1. Supports three search modes: keyword, vector, or hybrid (keyword + vector)
2. Embeds query text using Azure OpenAI for vector/hybrid modes
3. Queries Azure AI Search index with optional filters (video_id, time range)
4. Returns ranked results with segment metadata

Architecture Role:
- Query endpoint for the search interface (Streamlit UI)
- Enables semantic search over transcribed video content
- Supports filtering by video and time ranges

Input: POST with:
  - q: Search query text (required)
  - mode: "keyword", "vector", or "hybrid" (default: "keyword")
  - top: Number of results to return (1-50)
  - k: Vector recall depth for vector/hybrid modes
  - video_id, start_ms, end_ms: Optional filters

Output: JSON with search mode, total count, and ranked hits array
"""

import json
import os
from typing import Any, Dict, List, Optional

import azure.functions as func
import requests

DEFAULT_SEARCH_API_VERSION = "2024-05-01-preview"
DEFAULT_EMBED_API_VERSION = "2024-10-21"


def _escape_odata_string(value: str) -> str:
    return value.replace("'", "''")


def _build_filter(
    video_id: Optional[str],
    start_ms: Optional[int],
    end_ms: Optional[int],
    labels: Optional[List[str]] = None,
    label_match: str = "any",
) -> Optional[str]:
    clauses = ["video_id ne null"]
    if video_id:
        clauses.append(f"video_id eq '{_escape_odata_string(video_id)}'")
    if start_ms is not None:
        clauses.append(f"start_ms ge {int(start_ms)}")
    if end_ms is not None:
        clauses.append(f"end_ms le {int(end_ms)}")
    if labels:
        escaped = [_escape_odata_string(l) for l in labels]
        if label_match == "all":
            for name in escaped:
                clauses.append(f"pred_labels/any(l: l eq '{name}')")
        else:  # "any"
            any_clause = " or ".join(f"l eq '{name}'" for name in escaped)
            clauses.append(f"pred_labels/any(l: {any_clause})")
    return " and ".join(clauses) if clauses else None


def _embed_query(text: str) -> List[float]:
    endpoint = os.environ["EMBEDDINGS_ENDPOINT"].rstrip("/")
    key = os.environ["EMBEDDINGS_KEY"]
    deployment = os.environ["EMBEDDINGS_DEPLOYMENT"]
    api_version = os.environ.get("EMBEDDINGS_API_VERSION", DEFAULT_EMBED_API_VERSION)

    url = f"{endpoint}/openai/deployments/{deployment}/embeddings?api-version={api_version}"
    r = requests.post(
        url,
        headers={"Content-Type": "application/json", "api-key": key},
        json={"input": [text]},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data["data"][0]["embedding"]


def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()

        q = (body.get("q") or body.get("search") or "").strip()
        labels = body.get("labels") or []
        label_match = (body.get("label_match") or "any").lower()

        if not q and not labels:
            return func.HttpResponse(
                json.dumps({"error": "Provide non-empty 'q' or at least one label filter."}),
                mimetype="application/json",
                status_code=400,
            )

        mode = (body.get("mode") or "keyword").lower()  # keyword | vector | hybrid
        top = int(body.get("top", 10))
        top = max(1, min(top, 100))
        skip = int(body.get("skip", 0))
        skip = max(0, skip)

        # vector recall depth; hybrid often benefits from higher k than top
        k = int(body.get("k", max(20, top * 4)))
        k = max(top, min(k, 200))

        video_id = body.get("video_id")
        start_ms = body.get("start_ms")
        end_ms = body.get("end_ms")

        search_endpoint = os.environ["SEARCH_ENDPOINT"].rstrip("/")
        index_name = os.environ.get("SEARCH_INDEX", "segments")
        api_key = os.environ["SEARCH_QUERY_KEY"]
        api_version = os.environ.get("SEARCH_API_VERSION", DEFAULT_SEARCH_API_VERSION)

        url = f"{search_endpoint}/indexes/{index_name}/docs/search?api-version={api_version}"

        # When no query text, force keyword mode with wildcard search
        if not q:
            mode = "keyword"

        payload: Dict[str, Any] = {
            "top": top,
            "skip": skip,
            "count": True,
            "select": "segment_key,video_id,segment_id,start_ms,end_ms,text,pred_labels,pred_label_details,source_url,source_type",
        }

        # Deterministic ordering when there's no relevance signal from a query
        if not q:
            payload["orderby"] = "video_id asc, start_ms asc"

        odata_filter = _build_filter(video_id, start_ms, end_ms, labels, label_match)
        if odata_filter:
            payload["filter"] = odata_filter

        # keyword-only
        if mode == "keyword":
            payload["search"] = q if q else "*"

        # vector-only
        elif mode == "vector":
            qvec = _embed_query(q)
            payload["search"] = "*"  # required by some clients; safe default
            payload["vectorQueries"] = [{
                "kind": "vector",
                "vector": qvec,
                "fields": "embedding",
                "k": k
            }]

        # hybrid (keyword + vector)
        elif mode == "hybrid":
            qvec = _embed_query(q)
            payload["search"] = q
            payload["vectorQueries"] = [{
                "kind": "vector",
                "vector": qvec,
                "fields": "embedding",
                "k": k
            }]

        else:
            return func.HttpResponse(
                json.dumps({"error": "mode must be one of: keyword, vector, hybrid"}),
                mimetype="application/json",
                status_code=400,
            )

        r = requests.post(
            url,
            headers={"Content-Type": "application/json", "api-key": api_key},
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()

        hits = []
        for item in data.get("value", []):
            hits.append({
                "segment_key": item.get("segment_key"),
                "video_id": item.get("video_id"),
                "segment_id": item.get("segment_id"),
                "start_ms": item.get("start_ms"),
                "end_ms": item.get("end_ms"),
                "text": item.get("text"),
                "pred_labels": item.get("pred_labels") or [],
                "pred_label_details": item.get("pred_label_details"),
                "source_url": item.get("source_url"),
                "source_type": item.get("source_type"),
                "score": item.get("@search.score"),
            })

        return func.HttpResponse(
            json.dumps({"mode": mode, "count": data.get("@odata.count"), "hits": hits}, ensure_ascii=False),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            mimetype="application/json",
            status_code=500,
        )
