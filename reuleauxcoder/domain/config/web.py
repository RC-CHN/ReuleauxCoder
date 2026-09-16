"""Web proxy configuration syntax and secret-safe presentation."""

from urllib.parse import urlsplit, urlunsplit


class WebProxyConfigError(ValueError):
    """A proxy setting cannot be used for this request."""


def validate_web_proxy(value: object) -> str:
    message = "web.proxy must be env, direct, or an HTTP/HTTPS/SOCKS5 proxy URL"
    if not isinstance(value, str):
        raise WebProxyConfigError(message)
    if value in {"env", "direct"}:
        return value
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https", "socks5", "socks5h"}
            and parsed.hostname
            and not any(
                character.isspace() or ord(character) < 32 for character in value
            )
            and (parsed.port is None or 1 <= parsed.port <= 65535)
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise WebProxyConfigError(message)
    return value


def display_web_proxy(value: object) -> str:
    try:
        proxy = validate_web_proxy(value)
    except WebProxyConfigError:
        return "<invalid proxy>"
    if proxy in {"env", "direct"}:
        return proxy
    parsed = urlsplit(proxy)
    host = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit(
        (parsed.scheme, ("***@" if "@" in parsed.netloc else "") + host, "", "", "")
    )
