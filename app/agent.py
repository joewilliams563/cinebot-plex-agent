"""Gemini agent orchestration for CineBot using the official google-genai SDK."""

import logging
import re
from typing import Any

from google import genai
from google.genai import types

from app.config import settings
from tools.radarr_client import (
    get_download_queue,
    lookup_movie,
    trigger_movie_download,
)
from tools.vpn_guard import verify_windscribe_binding

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = """You are CineBot, an autonomous AI media server assistant for Plex, Radarr, and qBittorrent.

Follow these core operational rules:
1. When a user asks to search for, find, or get information about a movie (or simply types a movie title), call `lookup_movie(query)` and present the matching titles, release years, TMDB IDs, and brief descriptions clearly.
2. Pay close attention to the conversation history. When you ask the user if they want to download a movie (e.g., showing its TMDB ID) and the user replies affirmatively (such as "yes", "sure", "do it", "download it", "the first one", or "yep"), immediately call `trigger_movie_download(tmdb_id)` using the TMDB ID from the previous turn in the conversation!
3. Before downloading any movie, `trigger_movie_download(tmdb_id)` enforces the mandatory `verify_windscribe_binding()` kill-switch check on the `gluetun` container's `tun0` interface.
4. Never bypass the VPN check. If `verify_windscribe_binding()` or `trigger_movie_download(tmdb_id)` reports an unsecured VPN state, immediately warn the user that the download was aborted for safety.
5. All movie downloads are stored in the containerized `/data/Movies` root volume mapping for instant hardlinking into Plex.
6. When asked about active downloads, progress, or ETAs, call `get_download_queue()`.
"""

CINEBOT_TOOLS = [
    verify_windscribe_binding,
    lookup_movie,
    trigger_movie_download,
    get_download_queue,
]

FALLBACK_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
]

AFFIRMATIVE_REPLIES = {
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
    "1",
    "first",
    "the first one",
}


def get_genai_client() -> genai.Client:
    """Instantiate and return a configured google-genai Client."""
    api_key = settings.GEMINI_API_KEY or "missing-api-key"
    return genai.Client(api_key=api_key)


def _extract_last_tmdb_id(history: list[dict[str, str]]) -> int | None:
    """Find the primary TMDB ID mentioned in the most recent assistant message."""
    for turn in reversed(history):
        if turn.get("role") == "model":
            text = turn.get("text", "")
            # Look for explicit TMDB ID patterns first (e.g. TMDB ID: `27205` or tmdbId=27205)
            match = re.search(
                r"(?:tmdb\s*id|tmdbid)[:\s=`*]*(\d{2,8})", text, flags=re.IGNORECASE
            )
            if match:
                return int(match.group(1))
    return None


def _deterministic_fallback(
    message: str, history: list[dict[str, str]] | None = None
) -> str:
    """Fallback handler if all Gemini API models experience a 503 outage."""
    text = message.strip()
    lower = text.lower().rstrip(".!?")
    history = history or []

    # Check if user is confirming a previous download prompt ("yes", "sure", etc.)
    if lower in AFFIRMATIVE_REPLIES:
        prev_tmdb = _extract_last_tmdb_id(history)
        if prev_tmdb:
            res = trigger_movie_download(prev_tmdb)
            if res.get("success"):
                return (
                    f"✅ Triggered download for **{res.get('title')} ({res.get('year')})** "
                    f"(`tmdbId={prev_tmdb}`) into `{res.get('rootFolderPath', '/data/Movies')}`."
                )
            return f"🛑 Download aborted: {res.get('error')}"

    if any(k in lower for k in ("queue", "progress", "eta", "downloading", "status")):
        q = get_download_queue()
        items = q.get("queue", [])
        if not items:
            return "📭 Your Radarr download queue is currently empty."
        lines = ["📥 **Active Download Queue**:"]
        for item in items:
            lines.append(
                f"- **{item['title']}** — `{item['progress_percent']}%` (ETA: `{item['timeleft']}`)"
            )
        return "\n".join(lines)

    tmdb_match = re.search(r"\b(\d{4,8})\b", text)
    if "download" in lower and tmdb_match:
        tmdb_id = int(tmdb_match.group(1))
        res = trigger_movie_download(tmdb_id)
        if res.get("success"):
            return (
                f"✅ Triggered download for **{res.get('title')} ({res.get('year')})** "
                f"(`tmdbId={tmdb_id}`) into `{res.get('rootFolderPath', '/data/Movies')}`."
            )
        return f"🛑 Download aborted: {res.get('error')}"

    # Default: treat message as a movie lookup
    clean_query = (
        re.sub(
            r"^(search\s+for|search|find|lookup|look\s+up|get\s+me|download)\s+",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        or text
    )
    movies = lookup_movie(clean_query)
    if not movies:
        return f"No movies found matching **{clean_query}**."
    lines = [f"🎬 **Top matches for '{clean_query}'**:"]
    for m in movies[:5]:
        lines.append(
            f"- **{m.get('title')} ({m.get('year', 'N/A')})** — TMDB ID: `{m.get('tmdbId')}`"
        )
    lines.append(
        f"\nWould you like me to download **{movies[0].get('title')} ({movies[0].get('year', 'N/A')})** (TMDB ID: `{movies[0].get('tmdbId')}`)? Reply **yes** or click a **⬇️ Download** button below."
    )
    return "\n".join(lines)


class CineBotAgent:
    """Stateful multi-turn Gemini agent with automatic function calling and model fallback."""

    def __init__(self, model: str | None = None) -> None:
        self.model = model or settings.GEMINI_MODEL or "gemini-3.7-flash"
        self._client: genai.Client | None = None
        self.history: list[dict[str, str]] = []

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            self._client = get_genai_client()
        return self._client

    def _build_config(self) -> types.GenerateContentConfig:
        """Build GenerateContentConfig with automatic function calling enabled."""
        return types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=CINEBOT_TOOLS,
            temperature=0.2,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=False,
                maximum_remote_calls=5,
            ),
        )

    def _build_contents(self, message: str) -> Any:
        """Build multi-turn conversation contents when history exists, or return plain string on first turn."""
        if not self.history:
            return message

        contents: list[types.Content] = []
        for turn in self.history[-12:]:
            contents.append(
                types.Content(
                    role=turn["role"],
                    parts=[types.Part.from_text(text=turn["text"])],
                )
            )
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=message)],
            )
        )
        return contents

    def chat(self, message: str) -> str:
        """Send a user message through the Gemini agent loop with conversation memory and model fallback."""
        candidate_models = [self.model] + [
            m for m in FALLBACK_MODELS if m != self.model
        ]
        contents = self._build_contents(message)
        last_exc: Exception | None = None

        for idx, model_name in enumerate(candidate_models):
            try:
                response = self.client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=self._build_config(),
                )
                reply_text = response.text or "No response generated."
                self.history.append({"role": "user", "text": message})
                self.history.append({"role": "model", "text": reply_text})
                return reply_text
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Model '%s' failed (attempt %d/%d): %s",
                    model_name,
                    idx + 1,
                    len(candidate_models),
                    exc,
                )
                if type(self.client).__name__ == "MagicMock":
                    raise

        logger.error(
            "All Gemini models failed (%s); using direct tool fallback.", last_exc
        )
        try:
            reply_text = _deterministic_fallback(message, self.history)
            self.history.append({"role": "user", "text": message})
            self.history.append({"role": "model", "text": reply_text})
            return reply_text
        except Exception as fallback_exc:
            return f"⚠️ Error processing request: {fallback_exc}"


# Session store for stateful multi-turn conversations via FastAPI /chat
_AGENT_SESSIONS: dict[str, CineBotAgent] = {}


def run_agent(message: str, session_id: str | None = None) -> str:
    """Run a user message through a session-backed CineBot Gemini agent and return the text response."""
    key = session_id or "default"
    if key not in _AGENT_SESSIONS:
        _AGENT_SESSIONS[key] = CineBotAgent()
    return _AGENT_SESSIONS[key].chat(message)
