import asyncio

import httpx
import pytest

from reuleauxcoder.domain.config.web import WebProxyConfigError
from reuleauxcoder.infrastructure import web_client


@pytest.fixture
def proxy_env(monkeypatch):
    for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.upper(), raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://secure-proxy.test:8080")
    monkeypatch.setenv("ALL_PROXY", "socks5://other-proxy.test:1080")
    monkeypatch.setenv("NO_PROXY", "internal.test,localhost")


def test_environment_routing_and_explicit_modes(proxy_env, monkeypatch):
    route = web_client.proxy_for_url
    assert route("env", "https://example.test") == "http://secure-proxy.test:8080"
    assert route("env", "http://example.test") == "socks5://other-proxy.test:1080"
    assert route("env", "https://sub.internal.test") is None
    assert route("direct", "https://example.test") is None
    assert (
        route("http://fixed.test:7890", "https://internal.test")
        == "http://fixed.test:7890"
    )
    monkeypatch.setenv("https_proxy", "lowercase.test:1234")
    assert route("env", "https://example.test") == "http://lowercase.test:1234"
    monkeypatch.setenv("NO_PROXY", "*")
    assert route("env", "https://example.test") is None


def test_client_reuses_routes_and_closes_them_after_redirects(proxy_env, monkeypatch):
    actual = httpx.AsyncClient
    created = []
    requests = []

    def make_client(**options):
        proxy = options.pop("proxy")
        assert options["trust_env"] is False

        def respond(request):
            requests.append((proxy, str(request.url)))
            return httpx.Response(200, text="ok")

        client = actual(**options, transport=httpx.MockTransport(respond))
        created.append(client)
        return client

    monkeypatch.setattr(web_client.httpx, "AsyncClient", make_client)

    async def run():
        async with web_client.WebClient("env", timeout=5) as client:
            for host in ("outside.test", "internal.test", "outside.test"):
                async with client.stream("GET", f"https://{host}") as response:
                    assert await response.aread() == b"ok"

    asyncio.run(run())
    assert [proxy for proxy, _ in requests] == [
        "http://secure-proxy.test:8080",
        None,
        "http://secure-proxy.test:8080",
    ]
    assert len(created) == 2
    assert all(client.is_closed for client in created)


def test_public_only_rejects_proxy_but_accepts_no_proxy_bypass(proxy_env, monkeypatch):
    requests = []
    actual = httpx.AsyncClient

    def make_client(**options):
        assert options["proxy"] is None
        return actual(
            **options,
            transport=httpx.MockTransport(
                lambda request: requests.append(request) or httpx.Response(200)
            ),
        )

    monkeypatch.setattr(web_client.httpx, "AsyncClient", make_client)

    async def run():
        async with web_client.WebClient("env", timeout=5, public_only=True) as client:
            with pytest.raises(WebProxyConfigError, match="cannot verify"):
                async with client.stream("GET", "https://outside.test"):
                    pass
            async with client.stream("GET", "https://internal.test"):
                pass

    asyncio.run(run())
    assert len(requests) == 1
