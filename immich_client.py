"""Immich integration — search and share family photos via Matrix."""
import logging
import tempfile
from pathlib import Path
from datetime import datetime

import httpx
import config

log = logging.getLogger("family-bot.immich")

_immich = config._cfg.get("immich", {})
BASE_URL = _immich.get("base_url", "").rstrip("/")
API_KEY = _immich.get("api_key", "")

_headers = {"x-api-key": API_KEY, "Accept": "application/json"}


def _get(endpoint, **kwargs):
    """GET request to Immich API."""
    r = httpx.get(f"{BASE_URL}{endpoint}", headers=_headers, timeout=15, **kwargs)
    r.raise_for_status()
    return r.json()


def _post(endpoint, json_data=None, **kwargs):
    """POST request to Immich API."""
    r = httpx.post(f"{BASE_URL}{endpoint}", headers=_headers, json=json_data, timeout=15, **kwargs)
    r.raise_for_status()
    return r.json()


def search_photos(query, limit=5):
    """Smart search for photos by text (uses CLIP embeddings).
    Examples: 'beach sunset', 'birthday cake', 'dog in the snow'"""
    try:
        data = _post("/search/smart", json_data={
            "query": query,
            "page": 1,
            "size": limit,
        })
        assets = data.get("assets", {}).get("items", [])
        return [_format_asset(a) for a in assets]
    except Exception as e:
        log.error(f"Immich search error: {e}")
        return []


def search_by_date(start_date, end_date=None, limit=10):
    """Search photos by date range. Dates as 'YYYY-MM-DD'."""
    try:
        params = {
            "takenAfter": f"{start_date}T00:00:00.000Z",
            "page": 1,
            "size": limit,
        }
        if end_date:
            params["takenBefore"] = f"{end_date}T23:59:59.999Z"

        data = _post("/search/metadata", json_data=params)
        assets = data.get("assets", {}).get("items", [])
        return [_format_asset(a) for a in assets]
    except Exception as e:
        log.error(f"Immich date search error: {e}")
        return []


def get_people():
    """List all recognized people."""
    try:
        data = _get("/people")
        people = data.get("people", [])
        return [
            {"id": p["id"], "name": p.get("name", "Unknown"), "count": p.get("thumbnailPath", "")}
            for p in people if p.get("name")
        ]
    except Exception as e:
        log.error(f"Immich people error: {e}")
        return []


def search_by_person(name, limit=10):
    """Find photos of a specific person by name."""
    try:
        people = get_people()
        match = None
        for p in people:
            if name.lower() in p["name"].lower():
                match = p
                break

        if not match:
            return []

        data = _get(f"/people/{match['id']}")
        assets = data.get("assets", [])[:limit]
        return [_format_asset(a) for a in assets]
    except Exception as e:
        log.error(f"Immich person search error: {e}")
        return []


def get_albums():
    """List all albums."""
    try:
        data = _get("/albums")
        return [
            {
                "id": a["id"],
                "name": a.get("albumName", "Untitled"),
                "count": a.get("assetCount", 0),
                "updated": a.get("updatedAt", ""),
            }
            for a in data
        ]
    except Exception as e:
        log.error(f"Immich albums error: {e}")
        return []


def get_album_photos(album_id, limit=10):
    """Get photos from a specific album."""
    try:
        data = _get(f"/albums/{album_id}")
        assets = data.get("assets", [])[:limit]
        return [_format_asset(a) for a in assets]
    except Exception as e:
        log.error(f"Immich album photos error: {e}")
        return []


def download_thumbnail(asset_id) -> str:
    """Download a photo thumbnail to a temp file. Returns file path."""
    try:
        r = httpx.get(
            f"{BASE_URL}/assets/{asset_id}/thumbnail",
            headers={"x-api-key": API_KEY},
            params={"size": "preview"},
            timeout=30,
        )
        r.raise_for_status()

        content_type = r.headers.get("content-type", "image/jpeg")
        ext = ".jpg" if "jpeg" in content_type else ".webp"
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        tmp.write(r.content)
        tmp.close()
        return tmp.name
    except Exception as e:
        log.error(f"Immich thumbnail download error: {e}")
        return None


def download_original(asset_id) -> str:
    """Download the original full-size photo to a temp file. Returns file path."""
    try:
        r = httpx.get(
            f"{BASE_URL}/assets/{asset_id}/original",
            headers={"x-api-key": API_KEY},
            timeout=60,
        )
        r.raise_for_status()

        content_type = r.headers.get("content-type", "image/jpeg")
        ext = ".jpg" if "jpeg" in content_type else ".png"
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        tmp.write(r.content)
        tmp.close()
        return tmp.name
    except Exception as e:
        log.error(f"Immich original download error: {e}")
        return None


def get_stats():
    """Get library statistics."""
    try:
        data = _get("/server/statistics")
        return {
            "photos": data.get("photos", 0),
            "videos": data.get("videos", 0),
            "usage_bytes": data.get("usage", 0),
        }
    except Exception as e:
        log.error(f"Immich stats error: {e}")
        return None


def _format_asset(asset):
    """Format an asset into a consistent dict."""
    taken = asset.get("localDateTime") or asset.get("createdAt", "")
    try:
        dt = datetime.fromisoformat(taken.replace("Z", "+00:00"))
        date_str = dt.strftime("%B %d, %Y %I:%M %p")
    except (ValueError, AttributeError):
        date_str = taken[:10] if taken else "Unknown date"

    return {
        "id": asset["id"],
        "date": date_str,
        "type": asset.get("type", "IMAGE"),
        "filename": asset.get("originalFileName", ""),
        "location": asset.get("exifInfo", {}).get("city", ""),
    }


def format_results(results, query=""):
    """Format search results for display in chat."""
    if not results:
        return f"No photos found{' for ' + repr(query) if query else ''}."

    lines = []
    if query:
        lines.append(f"Found {len(results)} photo(s) for '{query}':\n")
    else:
        lines.append(f"Found {len(results)} photo(s):\n")

    for i, r in enumerate(results, 1):
        location = f" — {r['location']}" if r.get("location") else ""
        lines.append(f"  {i}. {r['date']}{location}")
        lines.append(f"     {r['filename']}")

    lines.append("\nSay a number to see the photo, or 'all' for a collage.")
    return "\n".join(lines)
