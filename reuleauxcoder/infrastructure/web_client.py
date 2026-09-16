"""Shared web-tool routing; redirects and provider retries select their own route."""

from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import urlsplit
from urllib.request import getproxies_environment, proxy_bypass_environment

import httpx

from reuleauxcoder.domain.config.web import WebProxyConfigError, validate_web_proxy


def proxy_for_url(mode: str, url: str) -> str | None:
    validate_web_proxy(mode)
    if mode == "direct":
        return None
    if mode != "env":
        return mode
    proxies = getproxies_environment()
    target = urlsplit(url)
    if "*" in {
        host.strip() for host in proxies.get("no", "").split(",")
    } or proxy_bypass_environment(target.netloc.rsplit("@", 1)[-1], proxies):
        return None
    proxy = proxies.get(target.scheme) or proxies.get("all")
    if not proxy:
        return None
    proxy = proxy if "://" in proxy else "http://" + proxy
    return validate_web_proxy(proxy)


class WebClient:
    """Reuse one HTTP client per route, with explicit lifetime and no route fallback."""

    def __init__(self, proxy: str, *, timeout: float, public_only: bool = False):
        self.proxy = validate_web_proxy(proxy)
        self.timeout = timeout
        self.public_only = public_only
        self._stack = AsyncExitStack()
        self._clients: dict[str | None, httpx.AsyncClient] = {}

    async def __aenter__(self):
        await self._stack.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self._stack.__aexit__(*args)

    @asynccontextmanager
    async def stream(self, method: str, url: str, **kwargs):
        proxy = proxy_for_url(self.proxy, url)
        if proxy is not None and self.public_only:
            raise WebProxyConfigError(
                "web.allow_private_networks=false cannot verify destinations behind a proxy; "
                "use web.proxy=direct or NO_PROXY for this host, or explicitly allow private networks."
            )
        if proxy not in self._clients:
            # Resolve routing ourselves so the policy check and transport agree.
            # Certificate environment settings remain independent of proxy mode.
            client = httpx.AsyncClient(
                proxy=proxy,
                trust_env=False,
                follow_redirects=False,
                timeout=self.timeout,
                verify=httpx.create_ssl_context(trust_env=True),
            )
            self._clients[proxy] = await self._stack.enter_async_context(client)
        async with self._clients[proxy].stream(method, url, **kwargs) as response:
            yield response
