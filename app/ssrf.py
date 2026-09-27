"""Fail-closed callback URL safety (SSRF allowlist, scheme, port, IP ranges)."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from app import config


def host_matches(hostname: str, allowed_host: str) -> bool:
    return hostname == allowed_host or hostname.endswith(f".{allowed_host}")


def is_safe_callback_url(url: str) -> tuple[bool, str]:
    safe, reason, _addresses = inspect_callback_url(url)
    return safe, reason


def inspect_callback_url(url: str) -> tuple[bool, str, list[str]]:
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        allowed_schemes = ("https", "http") if config.CALLBACK_ALLOW_HTTP else ("https",)
        if parsed.scheme not in allowed_schemes:
            return False, "scheme_not_allowed", []
        if not hostname:
            return False, "hostname_required", []
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            return False, "userinfo_not_allowed", []
        try:
            port = parsed.port
        except ValueError:
            return False, "invalid_port", []
        if port not in (None, 80, 443):
            return False, "port_not_allowed", []
        if not config.CALLBACK_ALLOWED_HOSTS:
            return False, "allowed_hosts_not_configured", []
        if not any(
            host_matches(hostname, allowed) for allowed in config.CALLBACK_ALLOWED_HOSTS
        ):
            return False, "host_not_allowed", []
        if any(
            host_matches(hostname, own.lower().rstrip(".")) for own in config.ALLOWED_HOSTS
        ):
            return False, "own_host_not_allowed", []
        if hostname == "localhost" or hostname.endswith(".internal") or hostname.endswith(".local"):
            return False, "local_hostname_not_allowed", []

        literal_address = None
        try:
            literal_address = ipaddress.ip_address(hostname)
        except ValueError:
            pass
        addresses = [literal_address] if literal_address is not None else [
            ipaddress.ip_address(info[4][0])
            for info in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        ]
        for address in addresses:
            mapped = getattr(address, "ipv4_mapped", None)
            checked = mapped or address
            if (
                checked.is_private
                or checked.is_loopback
                or checked.is_link_local
                or checked.is_multicast
                or checked.is_reserved
                or checked.is_unspecified
                or not checked.is_global
            ):
                return False, "private_address_not_allowed", []
        return True, "ok", [str(address) for address in addresses]
    except (OSError, ValueError):
        return False, "dns_resolution_failed", []
