"""
SSRF guard for any server-side fetch of a user-supplied URL.

A single shared validator used by every place the bot downloads something on a
user's behalf (the creator URL importer, the PDF report renderer, ...). It
accepts only http(s) URLs whose host resolves exclusively to public, routable
IP addresses, blocking loopback, RFC1918 private ranges, link-local hosts
(including the 169.254.169.254 cloud-metadata endpoint), multicast, reserved,
and unspecified addresses. It is deliberately dependency-free and raises
``ValueError`` with a short human-readable reason on rejection.

Callers that fetch over HTTP must additionally re-invoke this for every
redirect hop (a redirect target is a fresh, attacker-influenced URL).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_INTERNAL_NAME_SUFFIXES = (".local", ".internal", ".localhost")
_INTERNAL_NAMES = {"localhost", "metadata.google.internal"}


def assert_public_http_url(url: str) -> None:
    """Raise ValueError unless ``url`` is an http(s) URL whose host resolves
    only to public IP addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("please send a valid public http/https link.")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("the link has no valid hostname.")
    if host in _INTERNAL_NAMES or host.endswith(_INTERNAL_NAME_SUFFIXES):
        raise ValueError("links to internal/private hosts are not allowed.")

    # Literal IP needs no DNS; still normalise/validate it.
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
        resolved = [(literal,)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError as exc:
            raise ValueError(
                f"the link's host could not be resolved ({host}).") from exc
        resolved = [(ipaddress.ip_address(info[4][0]),) for info in infos]

    saw_address = False
    for (ip,) in resolved:
        saw_address = True
        if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise ValueError(
                "links to private, loopback or internal-network addresses "
                "are not allowed.")
    if not saw_address:
        raise ValueError("the link's host did not resolve to any address.")


def is_safe_resource_url(url: str, *, schemes=("http", "https"),
                         resolve: bool = False) -> bool:
    """Boolean variant for embedding a user-supplied resource URL.

    With ``resolve=False`` (suitable for offline renderers such as WeasyPrint
    that must not depend on DNS at render time) it validates the scheme and
    rejects literal loopback/private/link-local IPs and internal hostnames,
    but does NOT require a DNS lookup for ordinary domain names. With
    ``resolve=True`` it behaves like :func:`assert_public_http_url` (full
    resolution check) and simply reports False instead of raising.
    """
    parsed = urlparse(url)
    if parsed.scheme not in schemes:
        return False
    if parsed.scheme == "data":
        # data: URIs carry no host; the caller decides the allowed media
        # types (e.g. only data:image for embedded markdown images).
        return True
    if not parsed.netloc:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in _INTERNAL_NAMES or host.endswith(_INTERNAL_NAME_SUFFIXES):
        return False
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if (literal.is_loopback or literal.is_private or literal.is_link_local
                or literal.is_multicast or literal.is_reserved
                or literal.is_unspecified):
            return False
        return True
    if resolve:
        try:
            assert_public_http_url(url)
        except ValueError:
            return False
    return True
