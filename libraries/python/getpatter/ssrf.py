"""Shared SSRF host-normalization for outbound-URL guards.

Centralises the IP-literal handling used by every outbound-URL validator so the
tool-executor guard and the server mirror cannot drift. They had drifted: both
recognised only *canonical* IP literals (dotted-quad IPv4, RFC-5952 IPv6), so
the equivalent spellings that ``getaddrinfo`` honours — decimal / octal / hex /
short IPv4, and IPv4-mapped IPv6 — slipped past the private-range checks and
connected to the very internal host the guard meant to block (including the
``169.254.169.254`` cloud-metadata endpoint).

The two helpers below fold every such spelling to a concrete
:class:`ipaddress` object and answer "is this address internal?" identically
for both validators.
"""

from __future__ import annotations

import ipaddress
import socket

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def resolve_literal_ip(host: str) -> IPAddress | None:
    """Return the IP literal *host* denotes, or ``None`` when *host* is a name.

    Folds the numeric encodings ``getaddrinfo`` (Python ``httpx`` /
    ``websockets``) accepts so the caller can range-check the address the
    client would actually connect to:

      * canonical dotted-quad IPv4 and RFC-5952 IPv6 (via :mod:`ipaddress`)
      * decimal / octal / hex / short IPv4 — ``2130706433``, ``0x7f.0.0.1``,
        ``0177.0.0.1``, ``127.1`` — via :func:`socket.inet_aton`

    A genuine DNS hostname (``example.com``) returns ``None``: it is resolved at
    request time, not here, to avoid a blocking synchronous lookup on the event
    loop.
    """
    host = host.strip("[]")
    if not host:
        return None
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # Non-canonical numeric IPv4 (decimal / octal / hex / short forms). These
    # raise above but resolve to a real IPv4 — fold them the same way the C
    # resolver does. inet_aton rejects genuine hostnames, leaving them as DNS.
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return None
    return ipaddress.IPv4Address(packed)


def is_internal_ip(addr: IPAddress) -> bool:
    """True when *addr* — or the IPv4 an IPv4-mapped IPv6 embeds — is private,
    loopback, link-local, reserved, or unspecified.

    Unwrapping ``ipv4_mapped`` matters on Python < 3.13, where the range flags
    on the IPv6 wrapper do not reflect the embedded IPv4 (so
    ``::ffff:169.254.169.254`` would otherwise read as public).
    """
    candidates: list[IPAddress] = [addr]
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        candidates.append(mapped)
    return any(
        a.is_private
        or a.is_loopback
        or a.is_link_local
        or a.is_reserved
        or a.is_unspecified
        for a in candidates
    )
