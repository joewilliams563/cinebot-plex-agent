# CineBot Technical Specification

## Target Architecture
1. `app/config.py`: Pydantic BaseSettings loading `.env`.
2. `tools/vpn_guard.py`: `verify_windscribe_binding()` using `qbittorrent-api` to inspect `app_preferences()` (`current_network_interface` / `current_interface_name`) and confirm it matches `VPN_INTERFACE` ("Windscribe", "tun0", or "wg0").
3. `tools/radarr_client.py`:
   - `lookup_movie(query: str)`: `GET /api/v3/movie/lookup?term={query}`
   - `trigger_movie_download(tmdb_id: int)`: Runs `verify_windscribe_binding()` first. Then calls `GET /api/v3/movie/lookup/tmdb?tmdbId={tmdb_id}`, constructs the `POST /api/v3/movie` payload with `monitored: True` and `addOptions.searchForMovie: True`.
   - `get_download_queue()`: `GET /api/v3/queue` to check active download percentage and ETA.
4. `app/agent.py`: Gemini agent orchestration using `google-genai` with automatic function calling wired to the tools above.
5. `app/main.py`: FastAPI backend exposing:
   - `POST /chat`: Accepts user message, runs Gemini agent loop, returns response.
   - `POST /webhooks/radarr`: Receives Radarr `Grab` and `Download` (Import) webhook payloads and broadcasts status updates.
6. `ui/chat_app.py`: Chainlit chat UI connected to the agent and displaying movie posters, year, VPN status badge, and download notifications.