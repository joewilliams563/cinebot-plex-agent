"""Radarr v3 API client with mandatory VPN kill-switch enforcement."""

import logging
from typing import Any

import requests

from app.config import settings
from tools.vpn_guard import VPNStatus, verify_windscribe_binding

logger = logging.getLogger(__name__)

DEFAULT_ROOT_FOLDER = "/data/Movies"


def _get_base_url() -> str:
    """Return normalized Radarr v3 API base URL."""
    base = (settings.RADARR_URL or "http://radarr:7878/api/v3").rstrip("/")
    if not base.endswith("/api/v3"):
        base = f"{base}/api/v3"
    return base


def _get_headers() -> dict[str, str]:
    """Return HTTP headers for authenticating with Radarr v3 API."""
    return {
        "X-Api-Key": settings.RADARR_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _extract_poster(movie: dict[str, Any]) -> str | None:
    """Extract remote poster URL from a Radarr movie object."""
    if movie.get("remotePoster"):
        return str(movie["remotePoster"])
    for img in movie.get("images", []) or []:
        if isinstance(img, dict) and img.get("coverType", "").lower() == "poster":
            url = img.get("remoteUrl") or img.get("url")
            if url:
                return str(url)
    return None


def lookup_movie(query: str) -> list[dict[str, Any]]:
    """Search Radarr (TMDB) for movies matching the given search query.

    Args:
        query: The movie title or search term to look up.

    Returns:
        A list of matching movie dictionaries containing title, year, tmdbId,
        overview, remotePoster, titleSlug, and images.
    """
    url = f"{_get_base_url()}/movie/lookup"
    response = requests.get(
        url,
        params={"term": query},
        headers=_get_headers(),
        timeout=15,
    )
    response.raise_for_status()
    raw_results = response.json() or []

    results: list[dict[str, Any]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": item.get("title", "Unknown Title"),
                "year": item.get("year"),
                "tmdbId": item.get("tmdbId"),
                "titleSlug": item.get("titleSlug", ""),
                "overview": item.get("overview", ""),
                "remotePoster": _extract_poster(item),
                "images": item.get("images", []),
                "monitored": item.get("monitored", False),
                "inLibrary": bool(item.get("id")),
            }
        )
    return results


def trigger_movie_download(tmdb_id: int) -> dict[str, Any]:
    """Add a movie to Radarr by its TMDB ID and immediately trigger indexer search and download.

    Enforces a mandatory VPN pre-flight check via `verify_windscribe_binding()` before
    contacting Radarr. If the VPN check fails, the download is aborted immediately.

    Args:
        tmdb_id: The TMDB numeric identifier for the movie to download.

    Returns:
        A dictionary describing the result of the download request or VPN abort.
    """
    vpn_status = verify_windscribe_binding()
    is_bound = (
        vpn_status.get("is_bound", vpn_status.get("bound", False))
        if isinstance(vpn_status, dict) and not isinstance(vpn_status, VPNStatus)
        else bool(vpn_status)
    )

    if not is_bound:
        error_msg = (
            vpn_status.get(
                "message",
                "CRITICAL VPN FAILURE: qBittorrent is not bound to tun0. Aborting download.",
            )
            if isinstance(vpn_status, dict)
            else "CRITICAL VPN FAILURE: qBittorrent is not bound to tun0. Aborting download."
        )
        logger.error("Aborting movie download for tmdbId=%s: %s", tmdb_id, error_msg)
        return {
            "success": False,
            "status": "aborted_vpn_unsecured",
            "tmdbId": tmdb_id,
            "error": error_msg,
        }

    base_url = _get_base_url()
    headers = _get_headers()

    # Fetch full movie details from Radarr's TMDB lookup endpoint
    lookup_url = f"{base_url}/movie/lookup/tmdb"
    lookup_resp = requests.get(
        lookup_url,
        params={"tmdbId": tmdb_id},
        headers=headers,
        timeout=15,
    )
    lookup_resp.raise_for_status()
    lookup_data = lookup_resp.json()
    movie_data = (
        lookup_data[0]
        if isinstance(lookup_data, list) and lookup_data
        else (lookup_data if isinstance(lookup_data, dict) else {})
    )

    root_folder = settings.PLEX_ROOT_FOLDER or DEFAULT_ROOT_FOLDER
    payload = {
        "title": movie_data.get("title", f"TMDB-{tmdb_id}"),
        "qualityProfileId": int(settings.QUALITY_PROFILE_ID),
        "titleSlug": movie_data.get("titleSlug", str(tmdb_id)),
        "images": movie_data.get("images", []),
        "tmdbId": int(movie_data.get("tmdbId", tmdb_id)),
        "year": movie_data.get("year", 0),
        "rootFolderPath": root_folder,
        "monitored": True,
        "addOptions": {
            "searchForMovie": True,
        },
    }

    post_url = f"{base_url}/movie"
    post_resp = requests.post(
        post_url,
        json=payload,
        headers=headers,
        timeout=20,
    )
    post_resp.raise_for_status()
    added_movie = post_resp.json()

    return {
        "success": True,
        "status": "queued",
        "tmdbId": payload["tmdbId"],
        "title": added_movie.get("title", payload["title"]),
        "year": added_movie.get("year", payload["year"]),
        "rootFolderPath": added_movie.get("rootFolderPath", root_folder),
        "monitored": added_movie.get("monitored", True),
        "remotePoster": _extract_poster(added_movie) or _extract_poster(movie_data),
        "movie": added_movie,
    }


def get_download_queue() -> dict[str, Any]:
    """Retrieve the active Radarr download queue with completion percentage and ETA.

    Returns:
        A dictionary containing totalRecords and the list of active queue items.
    """
    url = f"{_get_base_url()}/queue"
    response = requests.get(
        url,
        headers=_get_headers(),
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()

    records = data.get("records", []) if isinstance(data, dict) else (data or [])
    items: list[dict[str, Any]] = []

    for rec in records:
        if not isinstance(rec, dict):
            continue
        size = float(rec.get("size") or 0)
        sizeleft = float(rec.get("sizeleft") or 0)
        if size > 0:
            progress = round(max(0.0, min(100.0, ((size - sizeleft) / size) * 100.0)), 1)
        else:
            progress = 0.0

        items.append(
            {
                "id": rec.get("id"),
                "movieId": rec.get("movieId"),
                "title": rec.get("title", "Unknown Download"),
                "status": rec.get("status", "unknown"),
                "trackedDownloadState": rec.get("trackedDownloadState", ""),
                "progress_percent": progress,
                "timeleft": rec.get("timeleft", "Unknown"),
                "estimatedCompletionTime": rec.get("estimatedCompletionTime"),
                "size": size,
                "sizeleft": sizeleft,
                "protocol": rec.get("protocol", "torrent"),
                "downloadClient": rec.get("downloadClient", "qBittorrent"),
            }
        )

    return {
        "totalRecords": len(items),
        "queue": items,
        "items": items,
    }
