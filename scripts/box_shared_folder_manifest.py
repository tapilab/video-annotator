"""
scripts/box_shared_folder_manifest.py - Generate Video Manifest from Box

This script enumerates .m4a video files from a Box shared folder and generates
a manifest file (videos.jsonl) that lists all videos with their IDs and media URLs.
It creates open shared links for each file so Azure Speech Service can access them.
Architecture Role:
- Pre-processing step before video ingestion
- Generates videos.jsonl input file for import_videos.py
- Handles Box API authentication and folder traversal
- Creates publicly accessible download URLs for Speech Service
Usage:
  python scripts/box_shared_folder_manifest.py
Output:
  - videos.jsonl: One JSON object per line with video_id and media_url
Configuration (via .env):
  - BOX_SHARED_FOLDER_URL: Box shared folder link
  - BOX_TOKEN or BOX_ACCESS_TOKEN/BOX_REFRESH_TOKEN: Box authentication
  - OUT_PATH: Output file path (default: videos.jsonl)
  - RECURSIVE: Whether to traverse subfolders (default: 1)
"""

import json
import os
import requests
import time
from typing import Dict, Any, List, Optional
from box_auth import get_access_token

BOX_API = "https://api.box.com/2.0"

SHARED_FOLDER_URL = os.environ["BOX_SHARED_FOLDER_URL"]
print("BOX_SHARED_FOLDER_URL =", SHARED_FOLDER_URL)
OUT_PATH = os.environ.get("OUT_PATH", "videos.jsonl")
RECURSIVE = os.environ.get("RECURSIVE", "1") == "1"


def shared_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "BoxApi": f"shared_link={SHARED_FOLDER_URL}",
        "Content-Type": "application/json",
    }


def auth_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def resolve_shared_folder(token: str) -> Dict[str, Any]:
    url = f"{BOX_API}/shared_items"
    h = shared_headers(token)
    r = requests.get(url, headers=h, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} {r.text} (headers sent: {h.get('BoxApi')})")
    return r.json()


def list_folder_items(token: str, folder_id: str, limit: int = 1000, offset: int = 0) -> Dict[str, Any]:
    url = f"{BOX_API}/folders/{folder_id}/items"
    params = {"limit": limit, "offset": offset}
    r = requests.get(url, headers=shared_headers(token), params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def ensure_open_shared_link_for_file(token: str, file_id: str, max_retries: int = 3) -> Optional[str]:
    """
    Ensure file has an open shared link with retry logic for timeouts.
    """
    url = f"{BOX_API}/files/{file_id}"
    payload = {"shared_link": {"access": "open"}}
    params = {"fields": "shared_link"}
    
    for attempt in range(max_retries):
        try:
            # Increased timeout to 60 seconds
            r = requests.put(url, headers=auth_headers(token), params=params, json=payload, timeout=60)
            
            if r.status_code == 404:
                print(f"⚠️  Skipping file {file_id} (not found)")
                return None
            
            r.raise_for_status()
            data = r.json()
            sl = data.get("shared_link") or {}
            
            dl = sl.get("download_url")
            if dl:
                return dl
            if sl.get("url"):
                return sl["url"]
            
            raise RuntimeError(f"No shared_link returned for file {file_id}: {data}")
            
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1, 2, 4 seconds
                print(f"⏱️  Timeout on file {file_id}, retrying in {wait_time}s ({attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
            else:
                print(f"⚠️  Skipping file {file_id} (timeout after {max_retries} attempts)")
                return None
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                print(f"⚠️  Skipping file {file_id} (not found)")
                return None
            raise


def walk(token: str, folder_id: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    offset = 0
    limit = 1000
    while True:
        page = list_folder_items(token, folder_id, limit=limit, offset=offset)
        entries = page.get("entries", [])
        items.extend(entries)
        total = page.get("total_count", 0)
        offset += len(entries)
        if offset >= total or not entries:
            break
    return items


def load_existing_entries() -> set:
    """Load already processed file IDs to avoid duplicates."""
    processed = set()
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    video_id = entry.get("video_id", "")
                    if video_id.startswith("vid_"):
                        processed.add(video_id[4:])
                except json.JSONDecodeError:
                    continue
    return processed


def main():
    token = get_access_token()

    shared_folder = resolve_shared_folder(token)
    if shared_folder.get("type") != "folder":
        raise RuntimeError(f"Shared link did not resolve to a folder: {shared_folder.get('type')}")
    root_id = shared_folder["id"]

    # Load already processed files to resume
    processed_ids = load_existing_entries()
    print(f"Resuming: {len(processed_ids)} files already processed")

    queue = [root_id]
    out_count = len(processed_ids)
    skipped = 0
    new_files = 0

    with open(OUT_PATH, "a", encoding="utf-8") as f:
        while queue:
            fid = queue.pop(0)
            entries = walk(token, fid)

            for e in entries:
                et = e.get("type")
                name = (e.get("name") or "")
                lname = name.lower()

                if et == "folder" and RECURSIVE:
                    queue.append(e["id"])
                    continue

                if et != "file":
                    continue
                if not lname.endswith(".m4a"):
                    continue

                file_id = e["id"]
                
                # Skip if already processed
                if file_id in processed_ids:
                    continue
                
                video_id = f"vid_{file_id}"
                
                                # Make a per-file open shared link (Speech can fetch without auth).
                # If your org disallows open links, this will fail — then you’ll need Blob staging.
                

                file_link = ensure_open_shared_link_for_file(token, file_id)
                
                if file_link is None:
                    skipped += 1
                    continue

                media_url = file_link
                f.write(json.dumps({"video_id": video_id, "media_url": media_url, "video_name": name}) + "\n")
                f.flush()  # Ensure write is saved immediately
                out_count += 1
                new_files += 1
                
                if out_count % 10 == 0:
                    print(f"Wrote {out_count} entries total...")
                
                # Small delay to avoid rate limiting
                time.sleep(0.2)

    print(f"Done. Total: {out_count} entries (new: {new_files}, skipped: {skipped})")


if __name__ == "__main__":
    main()
