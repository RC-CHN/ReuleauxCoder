"""Configuration value parsing shared by validation and runtime adapters."""

import ipaddress
import re


def parse_relay_bind(value: str) -> tuple[str, int]:
    """Parse host:port (including bracketed IPv6), without resolving or binding."""
    message = "remote_exec.relay_bind must be host:port with a port from 0 to 65535"
    if not isinstance(value, str):
        raise ValueError(message)
    host, separator, port = value.rpartition(":")
    if not separator or not host or not re.fullmatch(r"[0-9]{1,5}", port):
        raise ValueError(message)
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ValueError(message) from None
    elif not re.fullmatch(r"[A-Za-z0-9_.-]+", host):
        raise ValueError(message)
    if int(port) > 65535:
        raise ValueError(message)
    return host, int(port)
