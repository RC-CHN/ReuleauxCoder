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
import json
import os
import threading
from typing import Any, TypeVar

import httpx

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


def _web_settings(tool: Tool) -> tuple[bool, str]:
    config = getattr(tool, "_agent_config", None)
    enabled = bool(getattr(config, "web_enabled", True))
    provider = str(getattr(config, "web_search_provider", "auto"))
    return enabled, provider


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


def _mcp_result_text(body: str) -> str:
    """Extract the first text content item from a JSON or SSE MCP response."""
    payload: dict[str, Any] | None = None
    trimmed = body.strip()
    if trimmed.startswith("{"):
        payload = json.loads(trimmed)
    else:
        for line in body.splitlines():
            if line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
                break
    if not isinstance(payload, dict):
        raise ValueError("search provider returned an unrecognized response")
    result = payload.get("result")
    if not isinstance(result, dict):
        error = payload.get("error")
        raise ValueError(f"search provider error: {error or 'missing result'}")
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
            return str(item["text"])
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
    url = _EXA_MCP_URL
    if api_key:
        url = f"{url}?exaApiKey={api_key}"
    request = client.stream(
        "POST",
        url,
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
        "to 30 s and is capped at 120 s. Use web_search for discovery queries."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "minLength": 1,
                "description": "The URL to fetch (must start with http:// or https://)",
            },
            "format": {
                "type": "string",
                "enum": ["markdown", "text", "html"],
                "description": "Output format (default: markdown)",
            },
            "timeout": {
                "type": "number",
                "exclusiveMinimum": 0,
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
        if not url.startswith(("http://", "https://")):
            return "url must start with http:// or https://"
        return None

    def execute(  # type: ignore[override]
        self,
        url: str,
        format: str = "markdown",
        timeout: float | None = None,
    ) -> str:
        enabled, _ = _web_settings(self)
        if not enabled:
            return (
                "Web access is disabled by configuration (web_enabled=false); "
                "ask the user to enable it before retrying."
            )
        seconds = min(
            max(float(timeout), 1.0) if timeout is not None else _DEFAULT_FETCH_TIMEOUT,
            _MAX_FETCH_TIMEOUT,
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
                    self._execute_async(url, format, seconds, headers),
                    cancellation=self.current_cancellation_signal(),
                    timeout=seconds,
                )
            )
        except _WebRequestCancelled:
            return f"Fetch cancelled for {url}"
        except (TimeoutError, asyncio.TimeoutError):
            return f"Fetch timed out after {seconds:.0f}s for {url}"
        except _ResponseTooLarge:
            return f"Fetch rejected for {url}: response exceeds the 5 MiB limit"
        except httpx.TransportError as error:
            return f"Fetch failed for {url}: {error}"

    async def _execute_async(
        self,
        url: str,
        format: str,
        seconds: float,
        headers: dict[str, str],
    ) -> str:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=seconds,
        ) as client:
            status_code, response_headers, body, encoding = await self._fetch_once(
                client,
                url,
                headers,
            )
            # Cloudflare bot challenges key on TLS fingerprints; retry once
            # with an honest UA so legitimate fetches are not challenged.
            if (
                status_code == 403
                and response_headers.get("cf-mitigated") == "challenge"
            ):
                status_code, response_headers, body, encoding = (
                    await self._fetch_once(
                        client,
                        url,
                        {**headers, "User-Agent": _HONEST_USER_AGENT},
                    )
                )

        if status_code >= 400:
            return f"Fetch failed for {url}: HTTP {status_code}"

        content_type = response_headers.get("content-type", "").split(";")[0].strip()
        if content_type.startswith("image/"):
            return (
                f"Fetched {url}: image ({content_type}, {len(body)} bytes). "
                "Image content is not rendered as text."
            )
        text = body.decode(encoding or "utf-8", errors="replace")
        if format == "html" or not content_type.startswith("text/html"):
            return text
        if format == "text":
            return _html_to_markdown(text)  # tag-free extraction
        return _html_to_markdown(text)

    @staticmethod
    async def _fetch_once(
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
    ) -> tuple[int, httpx.Headers, bytes, str | None]:
        request = client.stream("GET", url, headers=headers)
        async with request as response:
            if response.status_code >= 400:
                return (
                    response.status_code,
                    response.headers,
                    b"",
                    response.encoding,
                )
            body = await _read_limited_body(response, max_bytes=_MAX_RESPONSE_BYTES)
            return (
                response.status_code,
                response.headers,
                body,
                response.encoding,
            )


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
        "provider rate limits. Use web_fetch to retrieve a specific URL."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
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
    ) -> str:
        enabled, preference = _web_settings(self)
        if not enabled:
            return (
                "Web access is disabled by configuration (web_enabled=false); "
                "ask the user to enable it before retrying."
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
            return f"Web search cancelled for {query!r}"

    @staticmethod
    async def _execute_async(
        query: str, num_results: int, providers: list[str]
    ) -> str:
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
                    errors.append(f"{provider}: {error}")
                    continue
                return _format_hits(hits, provider, query)
        detail = "; ".join(errors) if errors else "no providers available"
        return f"Web search failed for {query!r}: {detail}"
