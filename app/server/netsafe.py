"""Outbound address policy for server-side fetches and notifications (SSRF).

Always refused: link-local (169.254.0.0/16, fe80::/10, includes the cloud metadata address), well-known metadata
addresses, unspecified, multicast and reserved ranges. Private ranges (10/8, 172.16/12, 192.168/16, loopback, ULA)
are allowed by default because MCCB is self-hosted and sources are often on the LAN; FETCH_BLOCK_PRIVATE=1 refuses
them too. FETCH_ALLOW_LINK_LOCAL=1 lifts the link-local refusal (not recommended)."""
from __future__ import annotations

import ipaddress
import socket

from .config import settings

METADATA = {ipaddress.ip_address(a) for a in ("169.254.169.254", "169.254.170.2", "100.100.100.200",
                                              "fd00:ec2::254", "fd00:ec2::23")}


class BlockedAddress(ValueError):
    """Destination refused by the address policy (or not resolvable)."""


def blocked_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in METADATA:
        return "cloud metadata address"
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return "unspecified, multicast or reserved address"
    if ip.is_link_local and not settings.fetch_allow_link_local:
        return "link-local address"
    if settings.fetch_block_private and (ip.is_private or ip.is_loopback):
        return "private address (FETCH_BLOCK_PRIVATE=1)"
    return None


def resolve(host: str, port: int) -> list[str]:
    """IPs of `host`, all checked against the policy. Raises BlockedAddress if any is refused: a name that
    resolves to a mix of allowed and refused addresses is refused as a whole."""
    host = host.strip("[]")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise BlockedAddress(f"cannot resolve {host}: {e}")
    ips: list[str] = []
    for fam, _t, _p, _c, sa in infos:
        ip = ipaddress.ip_address(sa[0].split("%")[0])
        why = blocked_reason(ip)
        if why:
            raise BlockedAddress(f"{host} resolves to a refused destination ({ip}: {why})")
        if str(ip) not in ips:
            ips.append(str(ip))
    if not ips:
        raise BlockedAddress(f"cannot resolve {host}")
    return ips
