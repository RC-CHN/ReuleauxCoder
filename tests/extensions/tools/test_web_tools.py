"""Tests for the web_fetch and web_search builtin tools."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from reuleauxcoder.domain.agent.tool_outcome import ToolOutcomeStatus
from reuleauxcoder.extensions.tools.builtin import builtin_tool_types
from reuleauxcoder.extensions.tools.builtin import web
from reuleauxcoder.extensions.tools.builtin.web import (
    WebFetchTool,
    WebSearchTool,
    _html_to_markdown,
    _mcp_result_text,
    _parse_exa_hits,
    _parse_parallel_hits,
    _provider_order,
    _SearchHit,
)


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        content: bytes = b"",
        text: str | None = None,
        encoding: str = "utf-8",
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
        self.content = content
        self._text = text
        self.encoding = encoding
        self.chunks = chunks
        self.chunks_yielded = 0
        self.request: httpx.Request | None = None

    @property
    def text(self) -> str:
        if self._text is not None:
            return self._text
        return self.content.decode(self.encoding, errors="replace")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = self.request or httpx.Request("GET", "https://example.invalid")
            httpx.Response(self.status_code, request=request).raise_for_status()

    async def aiter_bytes(self, chunk_size: int | None = None):
        del chunk_size
        payload = (
            self.content if self._text is None else self._text.encode(self.encoding)
        )
        chunks = self.chunks if self.chunks is not None else [payload]
        for chunk in chunks:
            self.chunks_yielded += 1
            yield chunk


class _FakeStream:
    def __init__(self, handler, method: str, url: str, kwargs: dict[str, Any]) -> None:
        self._handler = handler
        self._method = method
        self._url = url
        self._kwargs = kwargs

    async def __aenter__(self) -> _FakeResponse:
        response = self._handler(self._method, self._url, **self._kwargs)
        response.request = httpx.Request(
            self._method,
            self._url,
            params=self._kwargs.get("params"),
        )
        return response

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeClient:
    """Minimal httpx.Client stand-in routed by a test-supplied handler."""

    def __init__(self, handler) -> None:
        self._handler = handler

    def __call__(self, *args: Any, **kwargs: Any) -> "_FakeClient":
        return self

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    def stream(self, method: str, url: str, **kwargs: Any) -> _FakeStream:
        return _FakeStream(self._handler, method, url, kwargs)


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    monkeypatch.setattr(web.httpx, "AsyncClient", _FakeClient(handler))


def _tool_with_config(tool, **overrides):
    tool._agent_config = SimpleNamespace(
        web_enabled=overrides.get("web_enabled", True),
        web_search_provider=overrides.get("web_search_provider", "auto"),
    )
    return tool


def test_web_tools_are_registered() -> None:
    names = {tool.name for tool in builtin_tool_types()}
    assert "web_fetch" in names
    assert "web_search" in names


def test_fetch_preflight_rejects_non_http_schemes() -> None:
    tool = WebFetchTool()
    assert tool._preflight_validate(url="ftp://example.com") is not None
    assert tool._preflight_validate(url="https://example.com") is None
    outcome = tool.preflight_validate({"url": "gopher://example.com"})
    assert outcome is not None
    assert "http" in outcome.content


def test_approval_subjects_expose_url_and_query() -> None:
    fetch = WebFetchTool()
    search = WebSearchTool()
    assert fetch.approval_subjects({"url": "https://a.dev/x"}) == ("https://a.dev/x",)
    assert fetch.approval_subjects({"url": ""}) == ()
    assert search.approval_subjects({"query": "python"}) == ("python",)


def test_web_disabled_returns_guidance() -> None:
    fetch = _tool_with_config(WebFetchTool(), web_enabled=False)
    search = _tool_with_config(WebSearchTool(), web_enabled=False)
    fetch_outcome = fetch.execute("https://example.com")
    search_outcome = search.execute("anything")
    assert fetch_outcome.status is ToolOutcomeStatus.DENIED
    assert search_outcome.status is ToolOutcomeStatus.DENIED
    assert "disabled" in fetch_outcome.model_text
    assert "disabled" in search_outcome.model_text


def test_fetch_converts_html_to_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    page = (
        b"<html><body><h1>Title</h1><p>Hello <b>world</b></p>"
        b'<script>var x=1;</script><a href="https://a.dev">link</a></body></html>'
    )

    def handler(method: str, url: str, **_: Any) -> _FakeResponse:
        return _FakeResponse(
            headers={"content-type": "text/html; charset=utf-8"}, content=page
        )

    _patch_client(monkeypatch, handler)
    output = WebFetchTool().execute("https://example.com")
    assert output.status is ToolOutcomeStatus.SUCCEEDED
    assert "# Title" in output.model_text
    assert "Hello world" in output.model_text
    assert "[link](https://a.dev)" in output.model_text
    assert "var x=1" not in output.model_text


def test_fetch_rejects_oversized_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(method: str, url: str, **_: Any) -> _FakeResponse:
        return _FakeResponse(
            headers={
                "content-type": "text/plain",
                "content-length": str(6 * 1024 * 1024),
            },
            content=b"",
        )

    _patch_client(monkeypatch, handler)
    outcome = WebFetchTool().execute("https://example.com/big")
    assert outcome.status is ToolOutcomeStatus.FAILED
    assert "5 MiB" in outcome.model_text


def test_fetch_stops_streaming_as_soon_as_body_exceeds_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(
        headers={"content-type": "text/plain"},
        chunks=[
            b"x" * web._MAX_RESPONSE_BYTES,
            b"y",
            b"must-not-be-read",
        ],
    )

    def handler(method: str, url: str, **_: Any) -> _FakeResponse:
        return response

    _patch_client(monkeypatch, handler)
    outcome = WebFetchTool().execute("https://example.com/chunked")
    assert outcome.status is ToolOutcomeStatus.FAILED
    assert "5 MiB" in outcome.model_text
    assert response.chunks_yielded == 2


def test_fetch_cancellation_closes_the_active_request_promptly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = threading.Event()
    started = threading.Event()
    cancelled = threading.Event()

    class _BlockingResponse(_FakeResponse):
        async def aiter_bytes(self, chunk_size: int | None = None):
            del chunk_size
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            yield b""  # pragma: no cover - keeps this an async generator

    def handler(method: str, url: str, **_: Any) -> _FakeResponse:
        return _BlockingResponse(headers={"content-type": "text/plain"})

    def cancel_soon() -> None:
        assert started.wait(timeout=1)
        cancellation.set()

    _patch_client(monkeypatch, handler)
    threading.Thread(target=cancel_soon, daemon=True).start()
    tool = WebFetchTool()
    started_at = time.monotonic()
    with tool.execution_scope(cancellation):
        output = tool.execute("https://example.com/slow")

    assert output.status is ToolOutcomeStatus.CANCELLED
    assert "cancelled" in output.model_text
    assert time.monotonic() - started_at < 1
    assert cancelled.wait(timeout=1)


def test_fetch_reports_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(method: str, url: str, **_: Any) -> _FakeResponse:
        return _FakeResponse(status_code=404, content=b"nope")

    _patch_client(monkeypatch, handler)
    outcome = WebFetchTool().execute("https://example.com/missing")
    assert outcome.status is ToolOutcomeStatus.FAILED
    assert "HTTP 404" in outcome.model_text


def test_fetch_retries_cloudflare_challenge_with_honest_ua(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_agents: list[str] = []

    def handler(
        method: str, url: str, headers: dict[str, str] | None = None, **_: Any
    ) -> _FakeResponse:
        seen_agents.append((headers or {}).get("User-Agent", ""))
        if len(seen_agents) == 1:
            return _FakeResponse(
                status_code=403, headers={"cf-mitigated": "challenge"}
            )
        return _FakeResponse(headers={"content-type": "text/plain"}, content=b"ok")

    _patch_client(monkeypatch, handler)
    outcome = WebFetchTool().execute("https://example.com")
    assert outcome.status is ToolOutcomeStatus.SUCCEEDED
    assert outcome.model_text == "ok"
    assert seen_agents[1] == web._HONEST_USER_AGENT


def test_html_extractor_skips_noise_and_keeps_structure() -> None:
    markdown = _html_to_markdown(
        "<h2>Head</h2><style>.a{}</style><ul><li>one</li><li>two</li></ul>"
    )
    assert "## Head" in markdown
    assert "- one" in markdown
    assert "- two" in markdown
    assert ".a{}" not in markdown


def test_mcp_result_text_supports_json_and_sse() -> None:
    json_body = '{"result": {"content": [{"type": "text", "text": "hits"}]}}'
    sse_body = 'event: message\ndata: {"result": {"content": [{"type": "text", "text": "hits"}]}}\n'
    assert _mcp_result_text(json_body) == "hits"
    assert _mcp_result_text(sse_body) == "hits"
    with pytest.raises(ValueError):
        _mcp_result_text("not a payload")


def test_exa_hit_parsing_normalizes_blocks() -> None:
    text = (
        "Title: First\nURL: https://a.dev\nPublished: 2026-01-01\nAuthor: N/A\n"
        "Highlights:\nsome excerpt\n\n"
        "Title: Second\nURL: https://b.dev\nPublished: N/A\n"
        "Highlights:\nanother excerpt\n"
    )
    hits = _parse_exa_hits(text)
    assert [hit.url for hit in hits] == ["https://a.dev", "https://b.dev"]
    assert hits[0].published == "2026-01-01"
    assert hits[1].published is None
    assert hits[0].excerpts


def test_parallel_hit_parsing_normalizes_json() -> None:
    text = (
        '{"search_id": "s1", "results": ['
        '{"url": "https://a.dev", "title": "A", "publish_date": null, '
        '"excerpts": ["one", "two"]}]}'
    )
    hits = _parse_parallel_hits(text)
    assert hits == [
        _SearchHit(title="A", url="https://a.dev", excerpts=("one", "two"))
    ]


def test_provider_order_rotation_and_pinning() -> None:
    assert _provider_order("exa") == ["exa"]
    assert _provider_order("parallel") == ["parallel"]
    first = _provider_order("auto")
    second = _provider_order("auto")
    assert first[0] != second[0]
    assert sorted(first) == ["exa", "parallel"]


def test_search_unifies_format_and_fails_over(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_exa(client: Any, query: str, num_results: int):
        raise httpx.TransportError("exa down")

    async def fine_parallel(
        client: Any, query: str, num_results: int
    ) -> list[_SearchHit]:
        return [
            _SearchHit(
                title="Doc", url="https://a.dev", published="2026", excerpts=("x",)
            )
        ]

    monkeypatch.setattr(web, "_search_exa", failing_exa)
    monkeypatch.setattr(web, "_search_parallel", fine_parallel)
    _patch_client(monkeypatch, lambda *a, **k: None)
    output = WebSearchTool().execute("query")
    assert output.status is ToolOutcomeStatus.SUCCEEDED
    assert "provider: parallel" in output.model_text
    assert "1. Doc" in output.model_text
    assert "URL: https://a.dev" in output.model_text
    assert "Published: 2026" in output.model_text
    assert "> x" in output.model_text


def test_search_reports_when_all_providers_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(client: Any, query: str, num_results: int):
        raise httpx.TransportError("down")

    monkeypatch.setattr(web, "_search_exa", broken)
    monkeypatch.setattr(web, "_search_parallel", broken)
    _patch_client(monkeypatch, lambda *a, **k: None)
    output = WebSearchTool().execute("query")
    assert output.status is ToolOutcomeStatus.FAILED
    assert "Web search failed" in output.model_text
    assert "TransportError" in output.model_text
    assert "down" not in output.model_text


def test_search_never_exposes_exa_key_from_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_key = "secret-demo-key"
    seen_params: list[dict[str, str] | None] = []

    def handler(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        seen_params.append(kwargs.get("params"))
        return _FakeResponse(status_code=401)

    monkeypatch.setenv("EXA_API_KEY", api_key)
    _patch_client(monkeypatch, handler)
    tool = _tool_with_config(WebSearchTool(), web_search_provider="exa")
    outcome = tool.execute("query")

    assert outcome.status is ToolOutcomeStatus.FAILED
    assert seen_params == [{"exaApiKey": api_key}]
    rendered = outcome.model_text + json.dumps(outcome.metadata)
    assert api_key not in rendered
    assert "HTTP 401" in rendered


def test_search_cancellation_stops_provider_failover_promptly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = threading.Event()
    started = threading.Event()
    cancelled = threading.Event()

    async def blocking_search(client: Any, query: str, num_results: int):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    def cancel_soon() -> None:
        assert started.wait(timeout=1)
        cancellation.set()

    monkeypatch.setattr(web, "_search_exa", blocking_search)
    _patch_client(monkeypatch, lambda *a, **k: None)
    threading.Thread(target=cancel_soon, daemon=True).start()
    tool = _tool_with_config(WebSearchTool(), web_search_provider="exa")
    started_at = time.monotonic()
    with tool.execution_scope(cancellation):
        output = tool.execute("query")

    assert output.status is ToolOutcomeStatus.CANCELLED
    assert "cancelled" in output.model_text
    assert time.monotonic() - started_at < 1
    assert cancelled.wait(timeout=1)


def test_search_calls_selected_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        calls.append(url)
        body = (
            '{"result": {"content": [{"type": "text", "text": '
            '"{\\"results\\": [{\\"url\\": \\"https://a.dev\\", '
            '\\"title\\": \\"A\\"}]}"}]}}'
        )
        return _FakeResponse(text=body)

    _patch_client(monkeypatch, handler)
    tool = _tool_with_config(WebSearchTool(), web_search_provider="parallel")
    output = tool.execute("query")
    assert calls == [web._PARALLEL_MCP_URL]
    assert output.status is ToolOutcomeStatus.SUCCEEDED
    assert "provider: parallel" in output.model_text
