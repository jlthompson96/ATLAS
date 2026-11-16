"""Which resolved addresses the proxy may connect to: public unicast only (DNS-rebinding defence).

An address is refused if it falls in any private, loopback, link-local, CGNAT, multicast, reserved,
documentation, translation (NAT64, 6to4, Teredo) or unspecified range, or if Python's own ``is_global``
says it is not global. The explicit table is the primary rule; ``is_global`` is a second opinion that can
only make the check stricter. IPv4-mapped IPv6 addresses are unwrapped and judged as IPv4.
"""

from __future__ import annotations

import ipaddress
from typing import Final

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

_FORBIDDEN_V4: Final = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",  # "this network"
        "10.0.0.0/8",  # RFC 1918
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local
        "172.16.0.0/12",  # RFC 1918
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # TEST-NET-1
        "192.88.99.0/24",  # 6to4 relay (deprecated)
        "192.168.0.0/16",  # RFC 1918
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved + limited broadcast
    )
)
_FORBIDDEN_V6: Final = tuple(
    ipaddress.ip_network(n)
    for n in (
        "::/128",  # unspecified
        "::1/128",  # loopback
        "64:ff9b::/96",  # NAT64 (embeds an IPv4 address we cannot judge)
        "64:ff9b:1::/48",  # local-use NAT64
        "100::/64",  # discard-only
        "2001::/23",  # IETF protocol assignments, includes Teredo 2001::/32
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4 (embeds an IPv4 address)
        "3fff::/20",  # documentation
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local
        "fec0::/10",  # site-local (deprecated)
        "ff00::/8",  # multicast
    )
)


def normalize(address: str) -> IPAddress | None:
    """Parse a resolver answer (scope ids stripped, IPv4-mapped unwrapped). None if it is not an address."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def parse_networks(text: str) -> tuple[IPNetwork, ...]:
    """Comma-separated CIDRs (the site's LAN prefixes). Raises ``ValueError`` on anything malformed."""
    return tuple(ipaddress.ip_network(part.strip()) for part in text.split(",") if part.strip())


def is_public_address(ip: IPAddress, extra_forbidden: tuple[IPNetwork, ...] = ()) -> bool:
    """Public unicast only. ``extra_forbidden`` is the site's own prefixes (for example the LAN's global IPv6
    prefix, which looks public but is the home network)."""
    for net in extra_forbidden:
        if net.version == ip.version and ip in net:
            return False
    table = _FORBIDDEN_V4 if isinstance(ip, ipaddress.IPv4Address) else _FORBIDDEN_V6
    if any(ip in network for network in table):
        return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local)
