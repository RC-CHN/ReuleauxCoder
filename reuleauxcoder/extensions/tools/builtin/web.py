"""Web access tools: retrieval (web_fetch) and discovery (web_search).

Both tools are read-only network clients. web_fetch pulls one URL directly;
web_search queries hosted search providers (Exa / Parallel) through their
stateless MCP-over-HTTP endpoints, unifies the results into one format, and
rotates providers per call with failover to the other. API keys are optional
environment overrides (EXA_API_KEY / PARALLEL_API_KEY) that raise provider
rate limits; the free tier works without them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from html.parser import HTMLParser
from ipaddress import IPv4Address, IPv6Address, ip_address
import json
import math
import os
import socket
import threading
from typing import Any, TypeVar
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import httpx

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.domain.cancellation import CancellationSignal
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import InterruptMode, Tool

_FETCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)
_HONEST_USER_AGENT = "reuleauxcoder-web/0.1"
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_MAX_SEARCH_RESPONSE_BYTES = 2 * 1024 * 1024
_RESPONSE_CHUNK_BYTES = 64 * 1024
_CANCELLATION_POLL_SECONDS = 0.05
_DEFAULT_FETCH_TIMEOUT = 30.0
_MAX_FETCH_TIMEOUT = 120.0
_SEARCH_TIMEOUT = 25.0
_MAX_REDIRECTS = 20
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_EXA_MCP_URL = "https://mcp.exa.ai/mcp"
_PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
_PROVIDERS = ("exa", "parallel")

_rotation_lock = threading.Lock()
_rotation_counter = 0
_T = TypeVar("_T")


class _ResponseTooLarge(ValueError):
    """Raised as soon as a streamed response crosses its decoded-byte limit."""


class _WebRequestCancelled(Exception):
    """Raised after the active HTTP task has acknowledged cancellation."""


class _InvalidWebUrl(ValueError):
    """Raised when a requested or redirected URL cannot be fetched safely."""


class _PrivateNetworkBlocked(ValueError):
    """Raised when public-only mode encounters a non-global target."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__("private or non-global network target blocked")


class _TargetResolutionFailed(ValueError):
    """Raised when public-only mode cannot resolve a target for validation."""


class _TooManyRedirects(ValueError):
    """Raised when a fetch exceeds its finite redirect budget."""


@dataclass(frozen=True)
class _FetchedResponse:
    status_code: int
    headers: httpx.Headers
    body: bytes
    encoding: str | None
    url: str


def _success(
    summary: str,
    content: str,
    **metadata: object,
) -> ToolOutcome:
    return ToolOutcome(
        status=ToolOutcomeStatus.SUCCEEDED,
        summary=summary,
        content=content,
        metadata=metadata,
    )


def _failure(
    summary: str,
    content: str,
    *,
    code: str,
    status: ToolOutcomeStatus = ToolOutcomeStatus.FAILED,
    error_kind: ToolErrorKind = ToolErrorKind.EXECUTION,
    **metadata: object,
) -> ToolOutcome:
    return ToolOutcome(
        status=status,
        summary=summary,
        content=content,
        error_kind=error_kind,
        metadata={"web_error_code": code, **metadata},
    )


def _safe_http_error(error: httpx.HTTPError, *, timeout: float) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        response = error.response
        phrase = response.reason_phrase.strip()
        suffix = f" {phrase}" if phrase else ""
        return f"HTTP {response.status_code}{suffix}"
    if isinstance(error, httpx.TimeoutException):
        return f"timed out after {timeout:g}s"
    return f"network transport failed ({type(error).__name__})"


def _safe_provider_error(error: Exception) -> str:
    if isinstance(error, _ResponseTooLarge):
        return "response exceeded the 2 MiB limit"
    if isinstance(error, httpx.HTTPError):
        return _safe_http_error(error, timeout=_SEARCH_TIMEOUT)
    if isinstance(error, json.JSONDecodeError):
        return "provider returned invalid JSON"
    if isinstance(error, ValueError):
        return "provider returned an invalid response"
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return f"timed out after {_SEARCH_TIMEOUT:g}s"
    return f"provider request failed ({type(error).__name__})"


async def _run_interruptible(
    coroutine: Coroutine[Any, Any, _T],
    *,
    cancellation: CancellationSignal | None,
    timeout: float | None,
) -> _T:
    task = asyncio.create_task(coroutine)
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    try:
        while True:
            if task.done():
                return task.result()
            if cancellation is not None and cancellation.is_set():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                raise _WebRequestCancelled

            wait_seconds = _CANCELLATION_POLL_SECONDS
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    raise TimeoutError
                wait_seconds = min(wait_seconds, remaining)

            done, _ = await asyncio.wait({task}, timeout=wait_seconds)
            if task in done:
                return task.result()
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def _read_limited_body(
    response: httpx.Response,
    *,
    max_bytes: int,
) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            raise _ResponseTooLarge

    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=_RESPONSE_CHUNK_BYTES):
        if len(body) + len(chunk) > max_bytes:
            raise _ResponseTooLarge
        body.extend(chunk)
    return bytes(body)


def _decode_response_body(response: httpx.Response, body: bytes) -> str:
    return body.decode(response.encoding or "utf-8", errors="replace")


def _web_settings(tool: Tool) -> tuple[bool, str, bool]:
    config = getattr(tool, "_agent_config", None)
    enabled = bool(getattr(config, "web_enabled", True))
    provider = str(getattr(config, "web_search_provider", "auto"))
    allow_private_networks = bool(
        getattr(config, "web_allow_private_networks", True)
    )
    return enabled, provider, allow_private_networks


def _parse_web_url(url: str) -> SplitResult:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise _InvalidWebUrl("URL contains an invalid host or port") from error
    if parsed.scheme.lower() not in {"http", "https"}:
        raise _InvalidWebUrl("URL scheme must be http or https")
    if not parsed.hostname:
        raise _InvalidWebUrl("URL must include a hostname")
    if any(character.isspace() for character in parsed.hostname):
        raise _InvalidWebUrl("URL hostname cannot contain whitespace")
    if port is not None and not 1 <= port <= 65535:
        raise _InvalidWebUrl("URL port must be between 1 and 65535")
    return parsed


def _display_url(url: str) -> str:
    """Return a diagnostic URL without credentials, query values, or fragments."""
    try:
        parsed = _parse_web_url(url)
    except _InvalidWebUrl:
        return "<invalid URL>"
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    safe = urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))
    return safe + ("?<redacted>" if parsed.query else "")


async def _resolve_host_addresses(
    host: str,
    port: int,
) -> tuple[IPv4Address | IPv6Address, ...]:
    try:
        literal = ip_address(host)
    except ValueError:
        try:
            records = await asyncio.get_running_loop().getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as error:
            raise _TargetResolutionFailed("target hostname could not be resolved") from error
        addresses: set[IPv4Address | IPv6Address] = set()
        for record in records:
            value = str(record[4][0]).split("%", 1)[0]
            try:
                addresses.add(ip_address(value))
            except ValueError:
                continue
        if not addresses:
            raise _TargetResolutionFailed(
                "target hostname did not resolve to an IP address"
            )
        return tuple(addresses)
    return (literal,)


async def _validate_network_target(url: str, *, allow_private: bool) -> None:
    parsed = _parse_web_url(url)
    if allow_private:
        return
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    addresses = await _resolve_host_addresses(host, port)
    if any(not address.is_global for address in addresses):
        raise _PrivateNetworkBlocked(url)


def _validate_connected_peer(response: httpx.Response, *, allow_private: bool) -> None:
    if allow_private:
        return
    stream = response.extensions.get("network_stream")
    get_extra_info = getattr(stream, "get_extra_info", None)
    if not callable(get_extra_info):
        return
    peer = get_extra_info("server_addr")
    if not isinstance(peer, tuple) or not peer:
        return
    try:
        address = ip_address(str(peer[0]).split("%", 1)[0])
    except ValueError:
        return
    if not address.is_global:
        raise _PrivateNetworkBlocked(str(response.request.url))


def _provider_order(preference: str) -> list[str]:
    """Rotation order for one call; explicit preference pins one provider."""
    global _rotation_counter
    if preference in _PROVIDERS:
        return [preference]
    with _rotation_lock:
        _rotation_counter += 1
        start = _rotation_counter % len(_PROVIDERS)
    return [*_PROVIDERS[start:], *_PROVIDERS[:start]]


class _MarkdownExtractor(HTMLParser):
    """Minimal HTML-to-Markdown extraction for fetched pages."""

    _SKIP_TAGS = {
        "script",
        "style",
        "noscript",
        "iframe",
        "object",
        "embed",
        "template",
        "svg",
    }
    _HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    _BLOCK_TAGS = {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "aside",
        "blockquote",
        "pre",
        "tr",
        "table",
        "ul",
        "ol",
        "hr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._heading_level = 0
        self._link_href: str | None = None
        self._link_text: list[str] = []

    def _emit(self, text: str) -> None:
        if self._link_href is not None:
            self._link_text.append(text)
        else:
            self._parts.append(text)

    def _newline(self, count: int = 2) -> None:
        self._emit("\n" * count)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self._HEADING_TAGS:
            self._heading_level = int(tag[1])
            self._newline()
            self._emit("#" * self._heading_level + " ")
        elif tag in self._BLOCK_TAGS or tag == "br":
            self._newline(1 if tag == "br" else 2)
        elif tag == "li":
            self._newline(1)
            self._emit("- ")
        elif tag == "a":
            href = dict(attrs).get("href")
            if href and href.startswith(("http://", "https://")):
                self._link_href = href
                self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in self._HEADING_TAGS:
            self._heading_level = 0
            self._newline()
        elif tag in self._BLOCK_TAGS:
            self._newline()
        elif tag == "a" and self._link_href is not None:
            text = "".join(self._link_text).strip()
            if text:
                self._parts.append(f"[{text}]({self._link_href})")
            else:
                self._parts.append(self._link_href)
            self._link_href = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if text:
            self._emit(text + " ")

    def markdown(self) -> str:
        lines = [line.strip() for line in "".join(self._parts).split("\n")]
        compacted: list[str] = []
        for line in lines:
            if line or (compacted and compacted[-1]):
                compacted.append(line)
        return "\n".join(compacted).strip()


def _html_to_markdown(html_text: str) -> str:
    parser = _MarkdownExtractor()
    parser.feed(html_text)
    parser.close()
    return parser.markdown()


class _TextExtractor(HTMLParser):
    """Tag-free HTML extraction that does not emit Markdown syntax."""

    _SKIP_TAGS = _MarkdownExtractor._SKIP_TAGS
    _BLOCK_TAGS = (
        _MarkdownExtractor._BLOCK_TAGS
        | _MarkdownExtractor._HEADING_TAGS
        | {"li", "br"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if not self._skip_depth and tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if not self._skip_depth and tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if text:
            self._parts.append(text + " ")

    def text(self) -> str:
        lines = [
            " ".join(line.split())
            for line in "".join(self._parts).splitlines()
        ]
        return "\n".join(line for line in lines if line).strip()


def _html_to_text(html_text: str) -> str:
    parser = _TextExtractor()
    parser.feed(html_text)
    parser.close()
    return parser.text()


def _sse_data_items(body: str) -> list[str]:
    items: list[str] = []
    data_lines: list[str] = []

    def flush() -> None:
        if data_lines:
            items.append("\n".join(data_lines))
            data_lines.clear()

    for line in body.splitlines():
        if not line:
            flush()
            continue
        if line.startswith(":"):
            continue
        if line == "data":
            data_lines.append("")
        elif line.startswith("data:"):
            value = line[len("data:") :]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    flush()
    return items


def _mcp_result_text(body: str) -> str:
    """Extract the matching text result from a JSON or SSE MCP response."""
    trimmed = body.strip()
    if trimmed.startswith("{"):
        candidates = [trimmed]
    else:
        candidates = _sse_data_items(body)

    parsed_payload = False
    last_json_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        if not candidate.strip() or candidate.strip() == "[DONE]":
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as error:
            last_json_error = error
            continue
        if not isinstance(payload, dict):
            continue
        parsed_payload = True
        if payload.get("id") not in {None, 1}:
            continue
        if payload.get("error") is not None:
            raise ValueError("search provider returned a JSON-RPC error")
        result = payload.get("result")
        if not isinstance(result, dict):
            continue
        for item in result.get("content") or []:
            if (
                isinstance(item, dict)
                and item.get("type") == "text"
                and item.get("text")
            ):
                return str(item["text"])
    if not parsed_payload:
        if last_json_error is not None:
            raise last_json_error
        raise ValueError("search provider returned an unrecognized response")
    raise ValueError("search provider returned no text content")


@dataclass(frozen=True)
class _SearchHit:
    title: str
    url: str
    published: str | None = None
    excerpts: tuple[str, ...] = field(default=())


def _parse_exa_hits(text: str) -> list[_SearchHit]:
    hits: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("Title: "):
            current = {
                "title": line[len("Title: ") :].strip(),
                "url": "",
                "published": None,
                "excerpts": [],
            }
            hits.append(current)
        elif current is None:
            continue
        elif line.startswith("URL: "):
            current["url"] = line[len("URL: ") :].strip()
        elif line.startswith("Published: "):
            published = line[len("Published: ") :].strip()
            current["published"] = None if published in {"", "N/A"} else published
        elif line.startswith(("Author: ", "Highlights:")):
            continue
        elif line.strip():
            current["excerpts"].append(line.strip())
    return [
        _SearchHit(
            title=str(hit["title"]),
            url=str(hit["url"]),
            published=hit["published"],
            excerpts=tuple(hit["excerpts"][:3]),
        )
        for hit in hits
        if hit["url"]
    ]


def _parse_parallel_hits(text: str) -> list[_SearchHit]:
    data = json.loads(text)
    hits: list[_SearchHit] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        hits.append(
            _SearchHit(
                title=str(item.get("title") or item["url"]),
                url=str(item["url"]),
                published=item.get("publish_date") or None,
                excerpts=tuple(
                    str(excerpt) for excerpt in (item.get("excerpts") or [])[:3]
                ),
            )
        )
    return hits


async def _search_exa(
    client: httpx.AsyncClient, query: str, num_results: int
) -> list[_SearchHit]:
    api_key = os.environ.get("EXA_API_KEY", "").strip()
    request = client.stream(
        "POST",
        _EXA_MCP_URL,
        params={"exaApiKey": api_key} if api_key else None,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "web_search_exa",
                "arguments": {
                    "query": query,
                    "numResults": num_results,
                    "type": "auto",
                    "livecrawl": "fallback",
                },
            },
        },
        headers={"Accept": "application/json, text/event-stream"},
        timeout=_SEARCH_TIMEOUT,
    )
    async with request as response:
        response.raise_for_status()
        body = await _read_limited_body(
            response,
            max_bytes=_MAX_SEARCH_RESPONSE_BYTES,
        )
        return _parse_exa_hits(_mcp_result_text(_decode_response_body(response, body)))


async def _search_parallel(
    client: httpx.AsyncClient, query: str, num_results: int
) -> list[_SearchHit]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "User-Agent": _HONEST_USER_AGENT,
    }
    api_key = os.environ.get("PARALLEL_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = client.stream(
        "POST",
        _PARALLEL_MCP_URL,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "web_search",
                "arguments": {
                    "objective": query,
                    "search_queries": [query],
                },
            },
        },
        headers=headers,
        timeout=_SEARCH_TIMEOUT,
    )
    async with request as response:
        response.raise_for_status()
        body = await _read_limited_body(
            response,
            max_bytes=_MAX_SEARCH_RESPONSE_BYTES,
        )
        return _parse_parallel_hits(
            _mcp_result_text(_decode_response_body(response, body))
        )[:num_results]


def _format_hits(hits: list[_SearchHit], provider: str, query: str) -> str:
    if not hits:
        return (
            f"No search results found for {query!r} (provider: {provider}). "
            "Try a different query."
        )
    lines = [f"Search results for {query!r} (provider: {provider}):", ""]
    for index, hit in enumerate(hits, 1):
        lines.append(f"{index}. {hit.title}")
        lines.append(f"   URL: {hit.url}")
        if hit.published:
            lines.append(f"   Published: {hit.published}")
        for excerpt in hit.excerpts:
            lines.append(f"   > {excerpt}")
        lines.append("")
    return "\n".join(lines).strip()


class WebFetchTool(Tool):
    effect_class = "network_read"
    parallel_safe = True
    interrupt_mode = InterruptMode.CANCEL_WITH_PARTIAL

    name = "web_fetch"
    description = (
        "Fetch one URL over HTTP and return its content. format controls the "
        "rendering: markdown (default) converts HTML, text strips tags, html "
        "returns the source. Responses are capped at 5 MiB; timeout defaults "
        "to 30 s and is capped at 120 s. Requests originate from the rcoder "
        "host. Use web_search for discovery queries."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "minLength": 1,
                "maxLength": 16384,
                "description": "The URL to fetch (must start with http:// or https://)",
            },
            "format": {
                "type": "string",
                "enum": ["markdown", "text", "html"],
                "description": "Output format (default: markdown)",
            },
            "timeout": {
                "type": "number",
                "minimum": 1,
                "maximum": 120,
                "description": "Optional timeout in seconds (max 120)",
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def approval_subjects(self, arguments) -> tuple[str, ...]:
        url = arguments.get("url")
        return (str(url),) if isinstance(url, str) and url else ()

    def _preflight_validate(  # type: ignore[override]
        self, url: str, **_
    ) -> str | None:
        try:
            _parse_web_url(url)
        except _InvalidWebUrl as error:
            return str(error)
        return None

    def execute(  # type: ignore[override]
        self,
        url: str,
        format: str = "markdown",
        timeout: float | None = None,
    ) -> ToolOutcome:
        enabled, _, allow_private_networks = _web_settings(self)
        if not enabled:
            return _failure(
                "Web access disabled",
                "Web access is disabled by configuration (web_enabled=false); "
                "ask the user to enable it before retrying.",
                code="disabled",
                status=ToolOutcomeStatus.DENIED,
                error_kind=ToolErrorKind.DENIED,
            )
        seconds = (
            float(timeout) if timeout is not None else _DEFAULT_FETCH_TIMEOUT
        )
        if not math.isfinite(seconds) or not 1 <= seconds <= _MAX_FETCH_TIMEOUT:
            return _failure(
                "Invalid web fetch timeout",
                "Web fetch timeout must be a finite number between 1 and 120 seconds.",
                code="invalid_timeout",
                error_kind=ToolErrorKind.INVALID_ARGUMENTS,
            )
        if format not in {"markdown", "text", "html"}:
            return _failure(
                "Invalid web fetch format",
                "Web fetch format must be one of markdown, text, or html.",
                code="invalid_format",
                error_kind=ToolErrorKind.INVALID_ARGUMENTS,
            )
        try:
            _parse_web_url(url)
        except _InvalidWebUrl as error:
            return _failure(
                "Invalid web URL",
                str(error),
                code="invalid_url",
                error_kind=ToolErrorKind.INVALID_ARGUMENTS,
            )
        accept = {
            "markdown": "text/markdown;q=1.0, text/x-markdown;q=0.9, "
            "text/plain;q=0.8, text/html;q=0.7, */*;q=0.1",
            "text": "text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1",
            "html": "text/html;q=1.0, application/xhtml+xml;q=0.9, */*;q=0.1",
        }[format]
        headers = {
            "User-Agent": _FETCH_USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            return asyncio.run(
                _run_interruptible(
                    self._execute_async(
                        url,
                        format,
                        seconds,
                        headers,
                        allow_private_networks=allow_private_networks,
                    ),
                    cancellation=self.current_cancellation_signal(),
                    timeout=seconds,
                )
            )
        except _WebRequestCancelled:
            return _failure(
                "Web fetch cancelled",
                "Web fetch was cancelled before completion.",
                code="cancelled",
                status=ToolOutcomeStatus.CANCELLED,
                error_kind=ToolErrorKind.INTERRUPTED,
            )
        except (TimeoutError, asyncio.TimeoutError):
            return _failure(
                "Web fetch timed out",
                f"Web fetch timed out after {seconds:g}s.",
                code="timeout",
                status=ToolOutcomeStatus.TIMED_OUT,
                error_kind=ToolErrorKind.INTERRUPTED,
                timeout_seconds=seconds,
            )
        except _PrivateNetworkBlocked as error:
            return _failure(
                "Private network target blocked",
                "Web fetch was blocked because web.allow_private_networks=false "
                "and the requested or redirected target is not globally routable.",
                code="private_network_blocked",
                status=ToolOutcomeStatus.DENIED,
                error_kind=ToolErrorKind.DENIED,
                blocked_url=_display_url(error.url),
            )
        except _TargetResolutionFailed:
            return _failure(
                "Web target validation failed",
                "Web fetch could not validate the target hostname while private "
                "network access is disabled.",
                code="target_resolution_failed",
            )
        except _TooManyRedirects:
            return _failure(
                "Too many web redirects",
                f"Web fetch stopped after {_MAX_REDIRECTS} redirects.",
                code="too_many_redirects",
                redirect_limit=_MAX_REDIRECTS,
            )
        except _ResponseTooLarge:
            return _failure(
                "Web response too large",
                "Web fetch stopped because the response exceeds the 5 MiB limit.",
                code="response_too_large",
                limit_bytes=_MAX_RESPONSE_BYTES,
            )
        except httpx.TransportError as error:
            return _failure(
                "Web fetch failed",
                f"Web fetch failed: {_safe_http_error(error, timeout=seconds)}.",
                code="transport_error",
                transport_error=type(error).__name__,
            )
        except httpx.InvalidURL:
            return _failure(
                "Invalid web URL",
                "The HTTP client rejected the requested or redirected URL.",
                code="invalid_url",
                error_kind=ToolErrorKind.INVALID_ARGUMENTS,
            )

    async def _execute_async(
        self,
        url: str,
        format: str,
        seconds: float,
        headers: dict[str, str],
        *,
        allow_private_networks: bool,
    ) -> ToolOutcome:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=seconds,
            trust_env=allow_private_networks,
        ) as client:
            response, redirect_count = await self._fetch_redirect_chain(
                client,
                url,
                headers,
                allow_private_networks=allow_private_networks,
            )
            # Cloudflare bot challenges key on TLS fingerprints; retry once
            # with an honest UA so legitimate fetches are not challenged.
            if (
                response.status_code == 403
                and response.headers.get("cf-mitigated") == "challenge"
            ):
                response, redirect_count = await self._fetch_redirect_chain(
                    client,
                    url,
                    {**headers, "User-Agent": _HONEST_USER_AGENT},
                    allow_private_networks=allow_private_networks,
                )

        common_metadata = {
            "requested_url": _display_url(url),
            "final_url": _display_url(response.url),
            "redirect_count": redirect_count,
        }
        if response.status_code >= 400:
            return _failure(
                "Web fetch failed",
                f"Web fetch failed: HTTP {response.status_code}.",
                code="http_status",
                http_status=response.status_code,
                **common_metadata,
            )

        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if content_type.startswith("image/"):
            return _success(
                "Fetched image metadata",
                f"Fetched an image ({content_type}, {len(response.body)} bytes). "
                "Image content is not rendered as text.",
                content_type=content_type,
                size_bytes=len(response.body),
                **common_metadata,
            )
        text = response.body.decode(response.encoding or "utf-8", errors="replace")
        if format == "html" or not content_type.startswith("text/html"):
            content = text
        elif format == "text":
            content = _html_to_text(text)
        else:
            content = _html_to_markdown(text)
        return _success(
            "Fetched web content",
            content,
            content_type=content_type or "unknown",
            size_bytes=len(response.body),
            format=format,
            **common_metadata,
        )

    @staticmethod
    async def _fetch_once(
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        *,
        allow_private_networks: bool,
    ) -> _FetchedResponse:
        request = client.stream("GET", url, headers=headers)
        async with request as response:
            _validate_connected_peer(
                response,
                allow_private=allow_private_networks,
            )
            is_redirect = (
                response.status_code in _REDIRECT_STATUSES
                and bool(response.headers.get("location"))
            )
            if response.status_code >= 400 or is_redirect:
                body = b""
            else:
                body = await _read_limited_body(
                    response,
                    max_bytes=_MAX_RESPONSE_BYTES,
                )
            return _FetchedResponse(
                status_code=response.status_code,
                headers=response.headers,
                body=body,
                encoding=response.encoding,
                url=url,
            )

    @classmethod
    async def _fetch_redirect_chain(
        cls,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        *,
        allow_private_networks: bool,
    ) -> tuple[_FetchedResponse, int]:
        current_url = url
        redirect_count = 0
        while True:
            await _validate_network_target(
                current_url,
                allow_private=allow_private_networks,
            )
            response = await cls._fetch_once(
                client,
                current_url,
                headers,
                allow_private_networks=allow_private_networks,
            )
            location = response.headers.get("location")
            if response.status_code not in _REDIRECT_STATUSES or not location:
                return response, redirect_count
            if redirect_count >= _MAX_REDIRECTS:
                raise _TooManyRedirects
            current_url = urljoin(current_url, location)
            redirect_count += 1


class WebSearchTool(Tool):
    effect_class = "network_read"
    parallel_safe = True
    interrupt_mode = InterruptMode.CANCEL_WITH_PARTIAL

    name = "web_search"
    description = (
        "Search the web for a query and return unified results (title, URL, "
        "publish date, excerpts). Rotates between hosted search providers "
        "(Exa, Parallel) per call and fails over to the other on errors. "
        "Optional EXA_API_KEY / PARALLEL_API_KEY environment variables raise "
        "provider rate limits. Requests originate from the rcoder host. Use "
        "web_fetch to retrieve a specific URL."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
                "description": "Web search query",
            },
            "num_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Number of results to return (default: 8)",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def approval_subjects(self, arguments) -> tuple[str, ...]:
        query = arguments.get("query")
        return (str(query),) if isinstance(query, str) and query else ()

    def execute(  # type: ignore[override]
        self, query: str, num_results: int = 8
    ) -> ToolOutcome:
        enabled, preference, _ = _web_settings(self)
        if not enabled:
            return _failure(
                "Web access disabled",
                "Web access is disabled by configuration (web_enabled=false); "
                "ask the user to enable it before retrying.",
                code="disabled",
                status=ToolOutcomeStatus.DENIED,
                error_kind=ToolErrorKind.DENIED,
            )
        providers = _provider_order(preference)
        try:
            return asyncio.run(
                _run_interruptible(
                    self._execute_async(query, num_results, providers),
                    cancellation=self.current_cancellation_signal(),
                    timeout=None,
                )
            )
        except _WebRequestCancelled:
            return _failure(
                "Web search cancelled",
                "Web search was cancelled before completion.",
                code="cancelled",
                status=ToolOutcomeStatus.CANCELLED,
                error_kind=ToolErrorKind.INTERRUPTED,
            )

    @staticmethod
    async def _execute_async(
        query: str, num_results: int, providers: list[str]
    ) -> ToolOutcome:
        searchers = {"exa": _search_exa, "parallel": _search_parallel}
        errors: list[str] = []
        async with httpx.AsyncClient() as client:
            for provider in providers:
                try:
                    hits = await asyncio.wait_for(
                        searchers[provider](client, query, num_results),
                        timeout=_SEARCH_TIMEOUT,
                    )
                except (
                    TimeoutError,
                    asyncio.TimeoutError,
                    httpx.HTTPError,
                    ValueError,
                    json.JSONDecodeError,
                ) as error:
                    errors.append(f"{provider}: {_safe_provider_error(error)}")
                    continue
                return _success(
                    (
                        f"Found {len(hits)} web search result(s)"
                        if hits
                        else "No web search results"
                    ),
                    _format_hits(hits, provider, query),
                    provider=provider,
                    result_count=len(hits),
                )
        detail = "; ".join(errors) if errors else "no providers available"
        return _failure(
            "Web search failed",
            f"Web search failed after provider attempts: {detail}.",
            code="providers_failed",
            provider_errors=tuple(errors),
        )
