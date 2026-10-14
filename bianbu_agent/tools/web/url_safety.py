from __future__ import annotations

import ipaddress
import socket
import urllib.parse

_SENSITIVE_QUERY_NAMES = frozenset(
    {"api_key", "apikey", "token", "access_token", "key", "secret", "password"}
)


def normalize_url_for_request(url: str) -> str:
    value = str(url).strip()
    parsed = urllib.parse.urlparse(value)
    if not parsed.scheme:
        value = "https://" + value
    return value


def sensitive_query_param_name(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    for name, _value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if name.lower() in _SENSITIVE_QUERY_NAMES:
            return name
    return None


def is_safe_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(normalize_url_for_request(url))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return False
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )
        }
    except OSError:
        return False
    return bool(addresses) and all(
        ipaddress.ip_address(value).is_global for value in addresses
    )


__all__ = ["is_safe_url", "normalize_url_for_request", "sensitive_query_param_name"]
