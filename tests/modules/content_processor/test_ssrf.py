from __future__ import annotations

import pytest

from src.modules.content_processor.scope.ssrf import IpClass, check_host, classify_ip


@pytest.mark.parametrize(
    ("addr", "expected"),
    [
        ("93.184.216.34", IpClass.PUBLIC),
        ("2606:2800:220:1:248:1893:25c8:1946", IpClass.PUBLIC),
        ("10.0.0.5", IpClass.PRIVATE),
        ("192.168.1.1", IpClass.PRIVATE),
        ("172.16.0.9", IpClass.PRIVATE),
        ("100.64.3.3", IpClass.PRIVATE),
        ("127.0.0.1", IpClass.LOOPBACK),
        ("::1", IpClass.LOOPBACK),
        ("169.254.169.254", IpClass.METADATA),
        ("169.254.10.10", IpClass.LINK_LOCAL),
        ("fe80::1", IpClass.LINK_LOCAL),
        ("224.0.0.1", IpClass.MULTICAST),
        ("0.0.0.0", IpClass.UNSPECIFIED),
        ("::ffff:10.1.1.1", IpClass.PRIVATE),
        ("not-an-ip", IpClass.INVALID),
    ],
)
def test_classify_ip(addr: str, expected: IpClass):
    assert classify_ip(addr) is expected


def test_public_host_allowed():
    d = check_host("example.com", resolver=lambda _h: ["93.184.216.34"])
    assert d.allowed and d.reason == "public"


def test_private_resolution_denied():
    d = check_host("intranet.local", resolver=lambda _h: ["10.1.2.3"])
    assert not d.allowed and d.reason == "denied:PRIVATE"


def test_mixed_answers_are_rebinding():
    d = check_host("evil.example", resolver=lambda _h: ["93.184.216.34", "127.0.0.1"])
    assert not d.allowed and d.reason == "rebinding_suspected"


def test_dns_failure_denied_not_raised():
    def boom(_h: str) -> list[str]:
        raise OSError("nxdomain")

    d = check_host("nope.example", resolver=boom)
    assert not d.allowed and d.reason.startswith("dns_failure")


def test_ip_literals_short_circuit():
    assert check_host("169.254.169.254").allowed is False
    assert check_host("93.184.216.34").allowed is True
