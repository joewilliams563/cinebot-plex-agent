"""Chainlit frontend for CineBot displaying VPN status badge, movie posters, and in-place live download progress."""

import asyncio
from datetime import datetime
import logging
from pathlib import Path
import re
import sys
from typing import Any

# Ensure workspace root is on sys.path when invoked via `chainlit run ui/chat_app.py`
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import chainlit as cl
import qbittorrentapi

from app.agent import CineBotAgent
from app.config import settings
from app.main import WEBHOOK_NOTIFICATIONS
from tools.radarr_client import (
    get_download_queue,
    lookup_movie,
    trigger_movie_download,
)
from tools.vpn_guard import verify_windscribe_binding

logger = logging.getLogger(__name__)

CONVERSATIONAL_REPLIES = {
    "yes",
    "y",
    "yeah",
    "yep",
    "yup",
    "sure",
    "ok",
    "okay",
    "do it",
    "download",
    "download it",
    "please",
    "yes please",
    "go ahead",
    "confirm",
    "no",
    "nope",
    "cancel",
    "stop",
    "thanks",
    "thank you",
    "1",
    "2",
    "3",
    "first",
    "the first one",
    "second",
    "the second one",
}


def format_vpn_badge(vpn_status: dict[str, Any]) -> str:
    """Render a Markdown VPN status badge from verify_windscribe_binding() output."""
    if vpn_status.get("is_bound"):
        iface = vpn_status.get("interface") or settings.VPN_INTERFACE
        ip_addr = vpn_status.get("ip") or "10.8.0.1"
        return f"🛡️ **VPN Status**: `🟢 SECURE ({iface} @ {ip_addr})`"
    return "🛡️ **VPN Status**: `🔴 UNSECURED — Kill-Switch Active (Downloads Blocked)`"


def _render_progress_bar(percent: float, width: int = 12) -> str:
    """Build a compact Unicode progress bar for the given percentage."""
    clamped = max(0.0, min(100.0, percent))
    filled = int(round((clamped / 100.0) * width))
    return "█" * filled + "░" * (width - filled)


def _format_eta(seconds: int | None, fallback: str = "Calculating...") -> str:
    """Convert qBittorrent ETA seconds into human-readable HH:MM:SS."""
    if seconds is None or seconds < 0 or seconds >= 8640000:
        return fallback
    hrs, rem = divmod(int(seconds), 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def _fetch_live_qbit_torrents() -> list[dict[str, Any]]:
    """Query qBittorrent directly for real-time torrent progress, speed, seeds, and state."""
    try:
        client = qbittorrentapi.Client(
            host=settings.QBIT_HOST,
            port=settings.QBIT_PORT,
            username=settings.QBIT_USER,
            password=settings.QBIT_PASS,
        )
        try:
            client.auth_log_in()
        except Exception:
            pass
        torrents = client.torrents_info()
        results: list[dict[str, Any]] = []
        for t in torrents:
            pct = round(float(getattr(t, "progress", 0.0)) * 100.0, 1)
            dlspeed_mb = round(float(getattr(t, "dlspeed", 0.0)) / (1024 * 1024), 2)
            eta_sec = getattr(t, "eta", None)
            results.append(
                {
                    "name": getattr(t, "name", "Unknown"),
                    "progress_percent": pct,
                    "state": getattr(t, "state", "downloading"),
                    "dlspeed_mb": dlspeed_mb,
                    "seeds": getattr(t, "num_seeds", 0),
                    "peers": getattr(t, "num_leechs", 0),
                    "eta": _format_eta(eta_sec),
                }
            )
        return results
    except Exception as exc:
        logger.debug("Live qBittorrent query skipped: %s", exc)
        return []


def format_queue_summary(
    queue_data: dict[str, Any],
    qbit_items: list[dict[str, Any]] | None = None,
) -> str:
    """Format active Radarr/qBittorrent download queue items into a clear Markdown summary."""
    qbit_items = qbit_items or []
    radarr_items = queue_data.get("queue", [])

    if qbit_items:
        lines = [f"📥 **Active Downloads ({len(qbit_items)})**:"]
        for t in qbit_items:
            pct = t["progress_percent"]
            bar = _render_progress_bar(pct)
            lines.append(
                f"- **{t['name']}**\n"
                f"  `{bar}` **{pct}%** | Speed: `{t['dlspeed_mb']} MB/s` | "
                f"Seeds: `{t['seeds']}` | State: `{t['state']}` | ETA: `{t['eta']}`"
            )
        return "\n".join(lines)

    if not radarr_items:
        return (
            "📡 **Radarr & qBittorrent Status**: No active downloads in queue "
            "(either searching indexers or already imported into `/data/Movies`)."
        )

    lines = [f"📥 **Active Downloads ({len(radarr_items)})**:"]
    for item in radarr_items:
        pct = float(item.get("progress_percent", 0.0))
        bar = _render_progress_bar(pct)
        lines.append(
            f"- **{item.get('title')}**\n"
            f"  `{bar}` **{pct}%** | State: `{item.get('trackedDownloadState') or item.get('status')}` "
            f"| ETA: `{item.get('timeleft', 'Calculating...')}`"
        )
    return "\n".join(lines)


async def flush_webhook_notifications() -> None:
    """Display any new Radarr Grab/Download webhook notifications in the chat session."""
    seen_count = cl.user_session.get("seen_notifications", 0)
    current_total = len(WEBHOOK_NOTIFICATIONS)
    if current_total > seen_count:
        new_items = WEBHOOK_NOTIFICATIONS[seen_count:current_total]
        cl.user_session.set("seen_notifications", current_total)
        for item in new_items:
            await cl.Message(
                author="Radarr Webhook",
                content=item.get("message", "Download status update received."),
            ).send()


@cl.on_chat_start
async def on_chat_start() -> None:
    """Initialize user session, verify Windscribe/Gluetun VPN binding, and greet the user."""
    cl.user_session.set("agent", CineBotAgent())
    cl.user_session.set("seen_notifications", len(WEBHOOK_NOTIFICATIONS))

    vpn_status = await asyncio.to_thread(verify_windscribe_binding)
    badge = format_vpn_badge(vpn_status)
    root_folder = settings.PLEX_ROOT_FOLDER or "/data/Movies"

    welcome_md = (
        f"# 🍿 Welcome to CineBot\n"
        f"{badge} | 📁 **Plex Root**: `{root_folder}`\n\n"
        "Ask me to search for a movie, download a title by name or TMDB ID, "
        "or check your active Radarr/qBittorrent download queue."
    )
    actions = [
        cl.Action(
            name="check_queue",
            payload={},
            label="🔄 Check Active Downloads",
        )
    ]
    welcome_msg = cl.Message(content=welcome_md, actions=actions)
    await welcome_msg.send()


@cl.action_callback("check_queue")
async def on_check_queue_action(action: cl.Action) -> None:
    """Update the existing download status message in-place instead of appending new messages."""
    vpn_status = await asyncio.to_thread(verify_windscribe_binding)
    badge = format_vpn_badge(vpn_status)
    queue_data, qbit_items = await asyncio.gather(
        asyncio.to_thread(get_download_queue),
        asyncio.to_thread(_fetch_live_qbit_torrents),
    )
    summary = format_queue_summary(queue_data, qbit_items)
    now_str = datetime.now().strftime("%I:%M:%S %p")

    header_prefix: str = cl.user_session.get("queue_header_prefix", "")
    prefix_block = f"{header_prefix}\n\n" if header_prefix else ""
    updated_content = (
        f"{badge} | 🕒 *Last refreshed: `{now_str}`*\n\n"
        f"{prefix_block}{summary}"
    )

    existing_msg: cl.Message | None = cl.user_session.get("queue_status_msg")
    if existing_msg is not None:
        existing_msg.content = updated_content
        await existing_msg.update()
    else:
        refresh_action = [
            cl.Action(
                name="check_queue",
                payload={},
                label="🔄 Refresh Download Status",
            )
        ]
        new_msg = cl.Message(content=updated_content, actions=refresh_action)
        await new_msg.send()
        cl.user_session.set("queue_status_msg", new_msg)

    await flush_webhook_notifications()


@cl.action_callback("download_movie")
async def on_download_action(action: cl.Action) -> None:
    """Handle one-click movie download button from search results with mandatory VPN check."""
    tmdb_id = int(action.payload.get("tmdbId", 0))
    title = action.payload.get("title", f"TMDB #{tmdb_id}")

    status_msg = cl.Message(
        content=f"⏳ **Verifying Gluetun VPN (`tun0`) & adding *{title}* (`tmdbId={tmdb_id}`) to Radarr...**"
    )
    await status_msg.send()

    result = await asyncio.to_thread(trigger_movie_download, tmdb_id)
    vpn_status = await asyncio.to_thread(verify_windscribe_binding)
    badge = format_vpn_badge(vpn_status)

    if result.get("success"):
        await asyncio.sleep(2.5)
        queue_data, qbit_items = await asyncio.gather(
            asyncio.to_thread(get_download_queue),
            asyncio.to_thread(_fetch_live_qbit_torrents),
        )
        queue_md = format_queue_summary(queue_data, qbit_items)
        now_str = datetime.now().strftime("%I:%M:%S %p")
        header_info = (
            f"✅ **Movie Added & Search Triggered**: **{result.get('title', title)} "
            f"({result.get('year', '')})** (`tmdbId={tmdb_id}`)\n"
            f"- **Plex Destination**: `{result.get('rootFolderPath', '/data/Movies')}`\n"
            f"- **Monitored**: `{result.get('monitored', True)}` | **Download Client**: `qBittorrent (Gluetun tun0)`"
        )
        cl.user_session.set("queue_header_prefix", header_info)
        msg = (
            f"{badge} | 🕒 *Last refreshed: `{now_str}`*\n\n"
            f"{header_info}\n\n"
            f"{queue_md}"
        )
        actions = [
            cl.Action(
                name="check_queue",
                payload={},
                label="🔄 Refresh Download Status",
            )
        ]
        status_msg.content = msg
        status_msg.actions = actions
        await status_msg.update()
        cl.user_session.set("queue_status_msg", status_msg)
    else:
        msg = (
            f"{badge}\n\n"
            f"🛑 **Download Aborted for {title}**: {result.get('error', 'VPN check failed.')}"
        )
        status_msg.content = msg
        status_msg.actions = []
        await status_msg.update()

    await flush_webhook_notifications()


def _extract_search_query(user_text: str) -> str | None:
    """Extract a movie search query from user text if the message looks like a movie lookup/request."""
    lower = user_text.lower().strip().rstrip(".!?")
    if not lower or lower in CONVERSATIONAL_REPLIES:
        return None
    if any(k in lower for k in ("queue", "status", "vpn", "progress", "eta", "help")):
        return None
    cleaned = re.sub(
        r"^(please\s+)?(can\s+you\s+)?(search\s+for|search|find|lookup|look\s+up|get\s+me|download|add|watch)\s+",
        "",
        user_text.strip(),
        flags=re.IGNORECASE,
    ).strip()
    if cleaned.lower().rstrip(".!?") in CONVERSATIONAL_REPLIES:
        return None
    return cleaned or user_text.strip()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """Process incoming chat messages, showing immediate Radarr search results before the LLM finishes."""
    await flush_webhook_notifications()

    agent: CineBotAgent = cl.user_session.get("agent") or CineBotAgent()
    user_text = message.content.strip()

    thinking_msg = cl.Message(content="⏳ *CineBot is checking Radarr & VPN status...*")
    await thinking_msg.send()

    vpn_status = await asyncio.to_thread(verify_windscribe_binding)
    badge = format_vpn_badge(vpn_status)

    elements: list[Any] = []
    actions: list[cl.Action] = []
    quick_preview_lines: list[str] = []

    query_term = _extract_search_query(user_text)
    if query_term:
        try:
            movies = await asyncio.to_thread(lookup_movie, query_term)
            if movies:
                quick_preview_lines.append(
                    f"🎬 **Found {len(movies)} matches in Radarr for '{query_term}'** *(top 3 shown below)*:"
                )
            for movie in movies[:3]:
                title = movie.get("title", "Unknown")
                year = movie.get("year", "N/A")
                tmdb_id = movie.get("tmdbId")
                in_lib = " *(Already in Radarr)*" if movie.get("inLibrary") else ""
                poster_url = movie.get("remotePoster")
                quick_preview_lines.append(
                    f"- **{title} ({year})** — TMDB ID: `{tmdb_id}`{in_lib}"
                )
                if poster_url:
                    elements.append(
                        cl.Image(
                            name=f"{title} ({year})",
                            url=poster_url,
                            display="inline",
                        )
                    )
                if tmdb_id:
                    actions.append(
                        cl.Action(
                            name="download_movie",
                            payload={"tmdbId": int(tmdb_id), "title": title},
                            label=f"⬇️ Download {title} ({year})",
                        )
                    )

            if quick_preview_lines:
                preview_md = "\n".join(quick_preview_lines)
                thinking_msg.content = (
                    f"{badge}\n\n{preview_md}\n\n⏳ *Generating AI summary...*"
                )
                thinking_msg.elements = elements
                thinking_msg.actions = actions
                await thinking_msg.update()
        except Exception as exc:
            logger.debug("Poster preview lookup skipped: %s", exc)

    try:
        reply_text = await asyncio.to_thread(agent.chat, user_text)
    except Exception as exc:
        logger.exception("Chat error")
        reply_text = f"⚠️ Error communicating with agent: `{exc}`"

    has_queue_button = False
    if "download" in reply_text.lower() or user_text.lower().strip() in CONVERSATIONAL_REPLIES:
        actions.append(
            cl.Action(
                name="check_queue",
                payload={},
                label="🔄 Refresh Download Status",
            )
        )
        has_queue_button = True

    thinking_msg.content = f"{badge}\n\n{reply_text}"
    thinking_msg.elements = elements
    thinking_msg.actions = actions
    await thinking_msg.update()

    if has_queue_button:
        cl.user_session.set("queue_header_prefix", reply_text)
        cl.user_session.set("queue_status_msg", thinking_msg)

    await flush_webhook_notifications()
