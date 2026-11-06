"""Configuration management for CineBot using pydantic-settings."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM Configuration
    GEMINI_API_KEY: str = Field(default="", description="Google Gemini API key")
    GEMINI_MODEL: str = Field(
        default="gemini-3.7-flash", description="Default Gemini model identifier"
    )

    # Radarr v3 Configuration
    RADARR_URL: str = Field(
        default="http://radarr:7878/api/v3", description="Radarr v3 API base URL"
    )
    RADARR_API_KEY: str = Field(default="", description="Radarr API key")
    PLEX_ROOT_FOLDER: str = Field(
        default="/data/Movies",
        description="Containerized root folder path for movies",
    )
    QUALITY_PROFILE_ID: int = Field(
        default=4, description="Radarr quality profile ID (e.g. 4 for HD-1080p)"
    )

    # qBittorrent + Windscribe / Gluetun Binding Check
    QBIT_HOST: str = Field(
        default="gluetun", description="qBittorrent host (running on gluetun container)"
    )
    QBIT_PORT: int = Field(default=8080, description="qBittorrent WebUI port")
    QBIT_USER: str = Field(default="admin", description="qBittorrent username")
    QBIT_PASS: str = Field(default="adminadmin", description="qBittorrent password")
    VPN_INTERFACE: str = Field(
        default="tun0",
        description="Expected VPN network interface (e.g. tun0, wg0, Windscribe)",
    )

    # Optional Direct Plex Refresh Fallback
    PLEX_URL: str = Field(
        default="http://host.docker.internal:32400", description="Plex Media Server URL"
    )
    PLEX_TOKEN: str = Field(default="", description="Plex authentication token")


settings = Settings()
