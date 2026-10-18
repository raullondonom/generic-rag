"""Authorization of the administration panel and its API.

With ADMIN_TOKEN set, admin requests must send it in the `X-Admin-Token` header. Without it, only
requests from the machine itself are allowed: loopback, or the Docker host through the bridge
gateway (requests coming through the ngrok tunnel or other containers are refused).
"""

from __future__ import annotations

import hmac
import ipaddress
import socket
import struct
from functools import lru_cache


@lru_cache
def docker_gateway() -> str | None:
    """IP of the default gateway of the container (the Docker host), read from /proc/net/route."""
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except OSError:
        return None
    return None


def is_local(host: str | None, gateway: str | None = None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or (gateway is not None and host == gateway)


def authorized(token_setting: str, sent_token: str | None, client_host: str | None,
               gateway: str | None = None) -> bool:
    if token_setting:
        return bool(sent_token) and hmac.compare_digest(sent_token.encode(), token_setting.encode())
    return is_local(client_host, gateway if gateway is not None else docker_gateway())
