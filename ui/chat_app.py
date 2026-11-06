"""Chainlit frontend for CineBot displaying VPN status badge, movie posters, and live download progress."""

import asyncio
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


def format_queue_summary(queue_data: dict[str, Any]) -> str:
    """Format active Radarr/qBittorrent download queue items into a clear Markdown summary."""
    items = queue_data.get("queue", [])
    if not items:
        return (
            "📡 **Radarr & qBittorrent Status**: Searching indexers (YTS / PirateBay / LimeTorrents via Prowlarr). "
            "Click **🔄 Refresh Download Status** below in a few seconds to check torrent progress & ETA."
        )
    lines = [f"📥 **Active Downloads ({len(items)})**:"]
    for item in items:
        lines.append(
            f"- **{item.get('title')}** — `{item.get('progress_percent', 0)}%` "
            f"| State: `{item.get('trackedDownloadState') or item.get('status')}` "
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
    await cl.Message(content=welcome_md, actions=actions).send()


@cl.action_callback("check_queue")
async def on_check_queue_action(action: cl.Action) -> None:
    """Display live Radarr/qBittorrent download progress and ETA when clicked."""
    vpn_status = await asyncio.to_thread(verify_windscribe_binding)
    badge = format_vpn_badge(vpn_status)
    queue_data = await asyncio.to_thread(get_download_queue)
    summary = format_queue_summary(queue_data)
    refresh_action = [
        cl.Action(
            name="check_queue",
            payload={},
            label="🔄 Refresh Download Status",
        )
    ]
    await cl.Message(content=f"{badge}\n\n{summary}", actions=refresh_action).send()
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
        queue_data = await asyncio.to_thread(get_download_queue)
        queue_md = format_queue_summary(queue_data)
        msg = (
            f"{badge}\n\n"
            f"✅ **Movie Added & Search Triggered**: **{result.get('title', title)} "
            f"({result.get('year', '')})** (`tmdbId={tmdb_id}`)\n"
            f"- **Plex Destination**: `{result.get('rootFolderPath', '/data/Movies')}`\n"
            f"- **Monitored**: `{result.get('monitored', True)}` | **Download Client**: `qBittorrent (Gluetun tun0)`\n\n"
            f"{queue_md}"
        )
        actions = [
            cl.Action(
                name="check_queue",
                payload={},
                label="🔄 Refresh Download Status",
            )
        ]
    else:
        msg = (
            f"{badge}\n\n"
            f"🛑 **Download Aborted for {title}**: {result.get('error', 'VPN check failed.')}"
        )
        actions = []

    status_msg.content = msg
    status_msg.actions = actions
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

            # Immediately update the message so the user sees posters & Download buttons in <0.5s!
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

    # Always attach a button to check active downloads when a download is discussed
    if "download" in reply_text.lower() or user_text.lower().strip() in CONVERSATIONAL_REPLIES:
        actions.append(
            cl.Action(
                name="check_queue",
                payload={},
                label="🔄 Refresh Download Status",
            )
        )

    thinking_msg.content = f"{badge}\n\n{reply_text}"
    thinking_msg.elements = elements
    thinking_msg.actions = actions
    await thinking_msg.update()

    await flush_webhook_notifications()
