"""Egress safety (cases 88/93): classify destinations before any connection.

The egress proxy is the second wall; this module is the first. Both must agree
that a destination is public before bytes are sent. Resolution is injectable so
tests never touch DNS.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from enum import StrEnum

from src.core.schemas import StrictModel

__all__ = ["EgressDecision", "IpClass", "check_host", "classify_ip", "system_resolver"]

Resolver = Callable[[str], list[str]]

_METADATA_NETS = (
    ipaddress.ip_network("169.254.169.254/32"),
    ipaddress.ip_network("fd00:ec2::254/128"),
    ipaddress.ip_network("100.100.100.200/32"),  # Alibaba Cloud metadata
)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class IpClass(StrEnum):
    """Classification of a resolved address."""

    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"
    LOOPBACK = "LOOPBACK"
    LINK_LOCAL = "LINK_LOCAL"
    METADATA = "METADATA"
    MULTICAST = "MULTICAST"
    RESERVED = "RESERVED"
    UNSPECIFIED = "UNSPECIFIED"
    INVALID = "INVALID"


class EgressDecision(StrictModel):
    """Whether a host may be contacted, and the evidence."""

    host: str
    allowed: bool
    resolved: list[str]
    classes: list[IpClass]
    reason: str


def classify_ip(address: str) -> IpClass:
    """Classify one IP literal. Unknown or malformed input is INVALID (denied)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return IpClass.INVALID
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if any(ip in net for net in _METADATA_NETS):
        return IpClass.METADATA
    if ip.is_unspecified:
        return IpClass.UNSPECIFIED
    if ip.is_loopback:
        return IpClass.LOOPBACK
    if ip.is_link_local:
        return IpClass.LINK_LOCAL
    if ip.is_multicast:
        return IpClass.MULTICAST
    if ip.is_private or (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT):
        return IpClass.PRIVATE
    if ip.is_reserved or not ip.is_global:
        return IpClass.RESERVED
    return IpClass.PUBLIC


def system_resolver(host: str) -> list[str]:
    """Resolve with the OS resolver; returns every address, both families."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({str(info[4][0]) for info in infos})


def check_host(host: str, resolver: Resolver = system_resolver) -> EgressDecision:
    """Decide whether `host` may be contacted.

    Every resolved address must be public. A mix of public and non-public
    answers is treated as a rebinding attempt and denied outright.
    """
    literal = classify_ip(host)
    if literal is not IpClass.INVALID:
        allowed = literal is IpClass.PUBLIC
        return EgressDecision(
            host=host,
            allowed=allowed,
            resolved=[host],
            classes=[literal],
            reason="public_literal" if allowed else f"denied_literal:{literal}",
        )
    try:
        resolved = resolver(host)
    except OSError as exc:
        return EgressDecision(
            host=host, allowed=False, resolved=[], classes=[], reason=f"dns_failure:{exc}"
        )
    if not resolved:
        return EgressDecision(host=host, allowed=False, resolved=[], classes=[], reason="dns_empty")
    classes = [classify_ip(addr) for addr in resolved]
    non_public = sorted({c for c in classes if c is not IpClass.PUBLIC})
    if non_public:
        mixed = len(non_public) < len(classes)
        reason = "rebinding_suspected" if mixed else f"denied:{','.join(non_public)}"
        return EgressDecision(
            host=host, allowed=False, resolved=resolved, classes=classes, reason=reason
        )
    return EgressDecision(
        host=host, allowed=True, resolved=resolved, classes=classes, reason="public"
    )
