"""FastAPI backend exposing CineBot chat and Radarr webhook endpoints."""

from datetime import datetime, timezone
import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import requests

from app.agent import run_agent
from app.config import settings
from tools.vpn_guard import verify_windscribe_binding

logger = logging.getLogger(__name__)

app = FastAPI(
    title="CineBot API & Webhook Server",
    description="Autonomous Plex/Radarr Media Agent with Mandatory Windscribe/Gluetun VPN Guard",
    version="1.0.0",
)

# In-memory broadcast log for Radarr webhook notifications consumed by UI/clients
WEBHOOK_NOTIFICATIONS: list[dict[str, Any]] = []


class ChatRequest(BaseModel):
    """Payload for POST /chat."""

    message: str = Field(..., min_length=1, description="User chat message for CineBot")
    session_id: str | None = Field(default=None, description="Optional chat session ID")


class ChatResponse(BaseModel):
    """Response returned from POST /chat."""

    response: str
    vpn_status: dict[str, Any]
    root_folder: str = Field(default="/data/Movies")


def _try_plex_refresh() -> bool:
    """Optionally trigger a Plex library scan if PLEX_TOKEN is configured."""
    token = (settings.PLEX_TOKEN or "").strip()
    if not token or token == "your_plex_token_here":
        return False
    try:
        from plexapi.server import PlexServer

        plex = PlexServer(settings.PLEX_URL, token, timeout=5)
        plex.library.update()
        return True
    except Exception as exc:
        logger.debug("Optional Plex library refresh skipped/failed: %s", exc)
        return False


def send_push_notification(title: str, body: str, tags: str = "popcorn,movie_camera") -> bool:
    """Send a push notification via ntfy.sh if NTFY_TOPIC is configured."""
    topic = (settings.NTFY_TOPIC or "").strip()
    if not topic:
        return False
    base_url = (settings.NTFY_URL or "https://ntfy.sh").rstrip("/")
    try:
        resp = requests.post(
            f"{base_url}/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": tags,
            },
            timeout=5,
        )
        return resp.ok
    except Exception as exc:
        logger.debug("ntfy push notification skipped/failed: %s", exc)
        return False


@app.get("/health")
def health_check() -> dict[str, Any]:
    """Return service health and current VPN binding status."""
    vpn = verify_windscribe_binding()
    return {
        "status": "ok",
        "vpn": dict(vpn),
        "root_folder": settings.PLEX_ROOT_FOLDER or "/data/Movies",
        "ntfy_topic": settings.NTFY_TOPIC,
    }


@app.post("/chat", response_model=ChatResponse)
def chat_endpoint(payload: ChatRequest) -> ChatResponse:
    """Accept a user message, run the Gemini agent loop with tool calling, and return the response."""
    try:
        reply = (
            run_agent(payload.message, payload.session_id)
            if payload.session_id
            else run_agent(payload.message)
        )
        vpn = verify_windscribe_binding()
        return ChatResponse(
            response=reply,
            vpn_status=dict(vpn),
            root_folder=settings.PLEX_ROOT_FOLDER or "/data/Movies",
        )
    except Exception as exc:
        logger.exception("Error processing /chat request")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/webhooks/radarr")
def radarr_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """Receive Radarr Grab and Download (Import) webhook payloads and broadcast status updates."""
    event_type = str(payload.get("eventType", "Unknown"))
    movie = payload.get("movie") or {}
    release = payload.get("release") or {}
    movie_file = payload.get("movieFile") or {}

    title = movie.get("title", "Unknown Movie")
    year = movie.get("year", "")
    tmdb_id = movie.get("tmdbId")
    folder_path = movie.get("folderPath") or (settings.PLEX_ROOT_FOLDER or "/data/Movies")
    year_suffix = f" ({year})" if year else ""

    if event_type == "Grab":
        quality = release.get("quality", "HD")
        indexer = release.get("indexer", "Prowlarr")
        message = (
            f"🎬 **Grabbed**: *{title}{year_suffix}* [{quality}] via {indexer}. "
            f"Downloading through Gluetun VPN (`{settings.VPN_INTERFACE}`)."
        )
        plex_refreshed = False
        send_push_notification(
            title=f"CineBot Grabbed: {title}{year_suffix}",
            body=f"Downloading {title}{year_suffix} [{quality}] via Gluetun VPN (tun0).",
            tags="clapper,arrow_down",
        )
    elif event_type in ("Download", "MovieFileImport"):
        file_path = movie_file.get("path") or folder_path
        plex_refreshed = _try_plex_refresh()
        message = (
            f"🎉 **Download Complete & Imported to Plex**: *{title}{year_suffix}* is now ready to watch in "
            f"`{file_path}`!"
        )
        send_push_notification(
            title=f"Ready on Plex: {title}{year_suffix}",
            body=f"{title}{year_suffix} has finished downloading and was imported to {file_path}!",
            tags="white_check_mark,popcorn",
        )
    elif event_type == "Test":
        message = "🔔 **Radarr Webhook Test**: Connection to CineBot verified."
        plex_refreshed = False
    else:
        message = f"ℹ️ **Radarr Event ({event_type})**: *{title}{year_suffix}*"
        plex_refreshed = False

    notification = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "eventType": event_type,
        "title": title,
        "year": year,
        "tmdbId": tmdb_id,
        "folderPath": folder_path,
        "message": message,
        "plexRefreshed": plex_refreshed,
    }
    WEBHOOK_NOTIFICATIONS.append(notification)
    logger.info("Broadcast Radarr webhook notification: %s", message)

    return {
        "status": "broadcasted",
        "eventType": event_type,
        "notification": notification,
    }


@app.get("/webhooks/notifications")
def get_webhook_notifications() -> dict[str, Any]:
    """Return broadcasted Radarr webhook notifications."""
    return {
        "count": len(WEBHOOK_NOTIFICATIONS),
        "notifications": list(WEBHOOK_NOTIFICATIONS),
    }
