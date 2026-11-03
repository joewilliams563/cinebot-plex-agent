"""Unit tests for CineBot mocking qBittorrent VPN states, Radarr API responses, and FastAPI endpoints."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.config import Settings, settings
from app.main import WEBHOOK_NOTIFICATIONS, app
from tools.radarr_client import (
    DEFAULT_ROOT_FOLDER,
    get_download_queue,
    lookup_movie,
    trigger_movie_download,
)
from tools.vpn_guard import VPNStatus, verify_windscribe_binding


def test_default_containerized_paths() -> None:
    """Verify default configuration paths point to /data/Movies, gluetun, and tun0."""
    default_cfg = Settings(_env_file=None)
    assert default_cfg.PLEX_ROOT_FOLDER == "/data/Movies"
    assert default_cfg.QBIT_HOST == "gluetun"
    assert default_cfg.VPN_INTERFACE == "tun0"
    assert DEFAULT_ROOT_FOLDER == "/data/Movies"


@patch("tools.vpn_guard.qbittorrentapi.Client")
def test_verify_windscribe_binding_success_tun0(mock_qbit_cls: MagicMock) -> None:
    """Verify VPN guard passes when qBittorrent is bound to tun0 with a valid VPN IP."""
    mock_client = MagicMock()
    mock_client.app_preferences.return_value = {
        "current_network_interface": "tun0",
        "current_interface_name": "tun0",
        "current_interface_address": "10.64.22.10",
    }
    mock_qbit_cls.return_value = mock_client

    result = verify_windscribe_binding()

    assert bool(result) is True
    assert result["is_bound"] is True
    assert result["interface"] == "tun0"
    assert result["ip"] == "10.64.22.10"
    mock_client.auth_log_in.assert_called_once()


@patch("tools.vpn_guard.qbittorrentapi.Client")
def test_verify_windscribe_binding_fails_wrong_interface(mock_qbit_cls: MagicMock) -> None:
    """Verify VPN guard fails when qBittorrent is bound to eth0 or unbound."""
    mock_client = MagicMock()
    mock_client.app_preferences.return_value = {
        "current_network_interface": "eth0",
        "current_interface_name": "eth0",
        "current_interface_address": "192.168.1.50",
    }
    mock_qbit_cls.return_value = mock_client

    with patch.object(settings, "VPN_INTERFACE", "tun0"):
        result = verify_windscribe_binding()

    assert bool(result) is False
    assert result["is_bound"] is False
    assert result["status"] == "unsecured"


@patch("tools.vpn_guard.qbittorrentapi.Client")
def test_verify_windscribe_binding_fails_invalid_ip(mock_qbit_cls: MagicMock) -> None:
    """Verify VPN guard fails if qBittorrent reports 0.0.0.0 or loopback IP on tun0."""
    mock_client = MagicMock()
    mock_client.app_preferences.return_value = {
        "current_network_interface": "tun0",
        "current_interface_name": "tun0",
        "current_interface_address": "0.0.0.0",
    }
    mock_qbit_cls.return_value = mock_client

    result = verify_windscribe_binding()

    assert bool(result) is False
    assert result["is_bound"] is False


@patch("tools.radarr_client.requests.get")
def test_lookup_movie_parses_radarr_results(mock_get: MagicMock) -> None:
    """Verify lookup_movie calls GET /api/v3/movie/lookup and parses movie metadata."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {
            "title": "Dune: Part Two",
            "year": 2024,
            "tmdbId": 693134,
            "titleSlug": "dune-part-two-693134",
            "overview": "Paul Atreides unites with Chani and the Fremen.",
            "images": [
                {
                    "coverType": "poster",
                    "remoteUrl": "https://image.tmdb.org/t/p/original/dune2.jpg",
                }
            ],
        }
    ]
    mock_get.return_value = mock_resp

    movies = lookup_movie("Dune")

    assert len(movies) == 1
    assert movies[0]["title"] == "Dune: Part Two"
    assert movies[0]["year"] == 2024
    assert movies[0]["tmdbId"] == 693134
    assert movies[0]["remotePoster"] == "https://image.tmdb.org/t/p/original/dune2.jpg"
    mock_get.assert_called_once()
    _, kwargs = mock_get.call_args
    assert kwargs["params"] == {"term": "Dune"}


@patch("tools.radarr_client.requests.post")
@patch("tools.radarr_client.requests.get")
@patch("tools.radarr_client.verify_windscribe_binding")
def test_trigger_movie_download_aborts_when_vpn_unbound(
    mock_vpn: MagicMock,
    mock_get: MagicMock,
    mock_post: MagicMock,
) -> None:
    """Verify trigger_movie_download aborts before calling Radarr if VPN is unbound."""
    mock_vpn.return_value = VPNStatus(
        is_bound=False,
        bound=False,
        status="unsecured",
        interface="eth0",
        ip=None,
        message="CRITICAL VPN FAILURE: qBittorrent is NOT securely bound.",
    )

    result = trigger_movie_download(693134)

    assert result["success"] is False
    assert result["status"] == "aborted_vpn_unsecured"
    mock_get.assert_not_called()
    mock_post.assert_not_called()


@patch("tools.radarr_client.requests.post")
@patch("tools.radarr_client.requests.get")
@patch("tools.radarr_client.verify_windscribe_binding")
def test_trigger_movie_download_succeeds_when_vpn_bound(
    mock_vpn: MagicMock,
    mock_get: MagicMock,
    mock_post: MagicMock,
) -> None:
    """Verify trigger_movie_download looks up TMDB metadata and POSTs to /api/v3/movie with /data/Movies."""
    mock_vpn.return_value = VPNStatus(
        is_bound=True,
        bound=True,
        status="secure",
        interface="tun0",
        ip="10.64.0.2",
        message="VPN Verified",
    )

    mock_lookup_resp = MagicMock()
    mock_lookup_resp.json.return_value = {
        "title": "Inception",
        "year": 2010,
        "tmdbId": 27205,
        "titleSlug": "inception-27205",
        "images": [{"coverType": "poster", "remoteUrl": "https://image.tmdb.org/inception.jpg"}],
    }
    mock_get.return_value = mock_lookup_resp

    mock_post_resp = MagicMock()
    mock_post_resp.json.return_value = {
        "id": 1,
        "title": "Inception",
        "year": 2010,
        "tmdbId": 27205,
        "rootFolderPath": "/data/Movies",
        "monitored": True,
    }
    mock_post.return_value = mock_post_resp

    with patch.object(settings, "PLEX_ROOT_FOLDER", "/data/Movies"):
        result = trigger_movie_download(27205)

    assert result["success"] is True
    assert result["title"] == "Inception"
    assert result["rootFolderPath"] == "/data/Movies"

    mock_post.assert_called_once()
    _, post_kwargs = mock_post.call_args
    sent_payload = post_kwargs["json"]
    assert sent_payload["tmdbId"] == 27205
    assert sent_payload["rootFolderPath"] == "/data/Movies"
    assert sent_payload["monitored"] is True
    assert sent_payload["addOptions"] == {"searchForMovie": True}


@patch("tools.radarr_client.requests.get")
def test_get_download_queue_calculates_progress(mock_get: MagicMock) -> None:
    """Verify get_download_queue computes progress percentage and returns ETA."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "totalRecords": 1,
        "records": [
            {
                "id": 42,
                "movieId": 1,
                "title": "Inception.2010.1080p.BluRay",
                "status": "downloading",
                "trackedDownloadState": "downloading",
                "size": 1000.0,
                "sizeleft": 250.0,
                "timeleft": "00:12:30",
            }
        ],
    }
    mock_get.return_value = mock_resp

    queue_data = get_download_queue()

    assert queue_data["totalRecords"] == 1
    item = queue_data["queue"][0]
    assert item["title"] == "Inception.2010.1080p.BluRay"
    assert item["progress_percent"] == 75.0
    assert item["timeleft"] == "00:12:30"


@patch("app.main.verify_windscribe_binding")
@patch("app.main.run_agent")
def test_fastapi_chat_and_radarr_webhook_endpoints(
    mock_run_agent: MagicMock,
    mock_vpn: MagicMock,
) -> None:
    """Verify FastAPI POST /chat and POST /webhooks/radarr endpoints."""
    client = TestClient(app)
    WEBHOOK_NOTIFICATIONS.clear()

    mock_run_agent.return_value = "Found Inception (2010) [tmdbId=27205]."
    mock_vpn.return_value = VPNStatus(
        is_bound=True,
        bound=True,
        status="secure",
        interface="tun0",
        ip="10.64.0.2",
        message="VPN Verified",
    )

    chat_resp = client.post("/chat", json={"message": "Search for Inception"})
    assert chat_resp.status_code == 200
    chat_json = chat_resp.json()
    assert "Inception" in chat_json["response"]
    assert chat_json["vpn_status"]["is_bound"] is True

    grab_resp = client.post(
        "/webhooks/radarr",
        json={
            "eventType": "Grab",
            "movie": {"title": "Inception", "year": 2010, "tmdbId": 27205},
            "release": {"quality": "Bluray-1080p", "indexer": "Prowlarr"},
        },
    )
    assert grab_resp.status_code == 200
    assert grab_resp.json()["eventType"] == "Grab"
    assert len(WEBHOOK_NOTIFICATIONS) == 1
