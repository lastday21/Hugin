from __future__ import annotations

import socket
from ipaddress import IPv4Address


def usable_source_ipv4(value: str | None) -> str | None:
    if value is None:
        return None
    source_ip = str(IPv4Address(value))
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((source_ip, 0))
    except OSError:
        return None
    finally:
        probe.close()
    return source_ip
