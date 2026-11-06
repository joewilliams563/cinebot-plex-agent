"""Gemini agent orchestration for CineBot using the official google-genai SDK."""

import logging
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
1. When a user asks to search for or find a movie, call `lookup_movie(query)` and present the matching titles, release years, TMDB IDs, and poster URLs clearly.
2. Before or when downloading any movie by its TMDB ID, always rely on `trigger_movie_download(tmdb_id)` which enforces the mandatory `verify_windscribe_binding()` kill-switch check on the `gluetun` container's `tun0` interface.
3. Never bypass the VPN check. If `verify_windscribe_binding()` or `trigger_movie_download(tmdb_id)` reports an unsecured VPN state, immediately warn the user that the download was aborted for safety.
4. All movie downloads are stored in the containerized `/data/Movies` root volume mapping for instant hardlinking into Plex.
5. When asked about active downloads, progress, or ETAs, call `get_download_queue()`.
"""

CINEBOT_TOOLS = [
    verify_windscribe_binding,
    lookup_movie,
    trigger_movie_download,
    get_download_queue,
]


def get_genai_client() -> genai.Client:
    """Instantiate and return a configured google-genai Client."""
    api_key = settings.GEMINI_API_KEY or "missing-api-key"
    return genai.Client(api_key=api_key)


class CineBotAgent:
    """Stateful or single-turn Gemini agent with automatic function calling."""

    def __init__(self, model: str | None = None) -> None:
        self.model = model or settings.GEMINI_MODEL or "gemini-3.8-flash"
        self._client: genai.Client | None = None

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

    def chat(self, message: str) -> str:
        """Send a user message through the Gemini agent loop and return the response text."""
        response = self.client.models.generate_content(
            model=self.model,
            contents=message,
            config=self._build_config(),
        )
        return response.text or "No response generated."


def run_agent(message: str) -> str:
    """Run a user message through the CineBot Gemini agent and return the text response."""
    agent = CineBotAgent()
    return agent.chat(message)
