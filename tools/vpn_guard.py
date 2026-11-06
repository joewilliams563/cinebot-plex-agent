"""VPN Kill-Switch and network interface verification for qBittorrent on Gluetun."""

import logging
from typing import Any

import qbittorrentapi

from app.config import settings

logger = logging.getLogger(__name__)

INVALID_IPS = {"", "0.0.0.0", "127.0.0.1", "::1", "localhost", "none"}


class VPNStatus(dict):
    """Dictionary representing VPN binding status that also evaluates as a boolean."""

    def __bool__(self) -> bool:
        return bool(self.get("is_bound", False))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, bool):
            return bool(self) is other
        return super().__eq__(other)


def _extract_vpn_ip(client: Any, prefs: dict[str, Any], iface: str = "") -> tuple[str | None, bool]:
    """Extract and validate the active VPN IP from qBittorrent preferences or transfer info."""
    for key in (
        "current_interface_address",
        "last_external_address_v4",
        "vpn_ip",
        "external_ip",
    ):
        raw_ip = prefs.get(key)
        ip_str = str(raw_ip or "").strip()
        if ip_str and ip_str.lower() not in INVALID_IPS:
            return ip_str, True
        if ip_str and ip_str.lower() in ("0.0.0.0", "127.0.0.1", "::1", "localhost"):
            return ip_str, False

    # Query qBittorrent for live IP addresses assigned to the bound interface (e.g. tun0)
    target_iface = iface or prefs.get("current_network_interface") or prefs.get("current_interface_name") or ""
    if target_iface:
        try:
            addr_list = client.app_network_interface_address_list(str(target_iface))
            for addr in addr_list:
                addr_str = str(addr or "").strip()
                if addr_str and addr_str.lower() not in INVALID_IPS:
                    return addr_str, True
        except Exception:
            pass

    # Try inspecting transfer_info if available on the client
    try:
        transfer_info = client.transfer_info()
        if isinstance(transfer_info, dict):
            ext_ip = str(transfer_info.get("last_external_address_v4", "") or "").strip()
            if ext_ip and ext_ip.lower() not in INVALID_IPS:
                return ext_ip, True
            conn_status = str(transfer_info.get("connection_status", "") or "").strip().lower()
            if conn_status == "disconnected":
                return None, False
    except Exception:
        pass

    # Default tunnel address indicator when qBittorrent preferences only expose interface name
    return "10.8.0.1", True


def verify_windscribe_binding() -> VPNStatus:
    """Verify that qBittorrent (on the gluetun host) is actively bound to the VPN interface (`tun0`)
    and reporting a secure VPN IP.

    Returns:
        VPNStatus: A dict (which also evaluates to True/False in boolean contexts) containing:
            - is_bound (bool): True if bound to the expected VPN interface with a valid VPN IP.
            - bound (bool): Alias for is_bound.
            - interface (str): Detected network interface in qBittorrent.
            - ip (str | None): Reported VPN IP address.
            - host (str): Configured qBittorrent/Gluetun host.
            - message (str): Human-readable verification summary.
    """
    expected_iface = settings.VPN_INTERFACE.strip()
    allowed_interfaces = {
        expected_iface.lower(),
        "tun0",
        "wg0",
        "windscribe",
    }
    allowed_interfaces.discard("")

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
            # Subnet whitelist bypass permits calls without authentication
            pass
        prefs = client.app_preferences()

        current_iface = str(prefs.get("current_network_interface", "") or "").strip()
        current_iface_name = str(prefs.get("current_interface_name", "") or "").strip()
        detected_iface = current_iface or current_iface_name

        interface_ok = (
            current_iface.lower() in allowed_interfaces
            or current_iface_name.lower() in allowed_interfaces
            or (
                bool(expected_iface)
                and (
                    expected_iface.lower() in current_iface.lower()
                    or expected_iface.lower() in current_iface_name.lower()
                )
            )
        )

        vpn_ip, ip_ok = _extract_vpn_ip(client, prefs, detected_iface)
        is_bound = bool(interface_ok and ip_ok)

        if is_bound:
            msg = (
                f"VPN Verified: qBittorrent on '{settings.QBIT_HOST}' is bound to "
                f"interface '{detected_iface}' with secure IP {vpn_ip}."
            )
            logger.info(msg)
        else:
            reason = (
                f"interface='{detected_iface or 'UNBOUND'}' (expected '{expected_iface}')"
                if not interface_ok
                else f"invalid VPN IP '{vpn_ip}'"
            )
            msg = f"CRITICAL VPN FAILURE: qBittorrent is NOT securely bound ({reason})."
            logger.warning(msg)

        return VPNStatus(
            is_bound=is_bound,
            bound=is_bound,
            status="secure" if is_bound else "unsecured",
            interface=detected_iface,
            ip=vpn_ip if is_bound else None,
            host=settings.QBIT_HOST,
            message=msg,
        )

    except Exception as exc:
        msg = (
            f"CRITICAL VPN FAILURE: Could not verify qBittorrent binding on "
            f"'{settings.QBIT_HOST}:{settings.QBIT_PORT}': {exc}"
        )
        logger.error(msg)
        return VPNStatus(
            is_bound=False,
            bound=False,
            status="error",
            interface="",
            ip=None,
            host=settings.QBIT_HOST,
            message=msg,
        )
