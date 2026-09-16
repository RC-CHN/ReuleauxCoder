# Web tool networking

`web_fetch` and `web_search` share the `web.proxy` setting. Omitting it means
`env`: read `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY` from the backend
process environment (lowercase forms are also accepted). Without a matching
proxy, requests go directly to the destination.

```yaml
web:
  proxy: env
```

Use `direct` to ignore environment proxies, or a URL to select a fixed proxy:

```yaml
web:
  proxy: "http://127.0.0.1:7890"
  # proxy: "socks5://127.0.0.1:1080"
  # proxy: direct
```

HTTP, HTTPS, SOCKS5 and SOCKS5H proxy URLs are accepted, including credentials.
A fixed URL applies to every web-tool request and ignores environment proxy
selection and `NO_PROXY`. Proxy failures do not trigger a direct retry. Search
provider failover still follows the configured route for each provider.

In `env` mode, proxy selection and `NO_PROXY` matching run again for each redirect
and provider URL. `NO_PROXY` accepts comma-separated host/domain names, optional
ports, and `*` for bypassing all proxies. Certificate configuration through
`SSL_CERT_FILE` / `SSL_CERT_DIR` is independent of proxy mode.

Place this setting in `~/.rcoder/config.yaml` or `.rcoder/config.yaml`; workspace
settings override user settings, and an explicit `--config` has higher priority.
`/config` shows `web.proxy` and its configuration source, with proxy credentials
masked. The setting affects only these two web tools, not model APIs, MCP servers,
shell commands or the remote execution peer.

Requests originate from the Python backend host. In a remote deployment,
`127.0.0.1` means that host, not the computer displaying the TUI.

`web.allow_private_networks` retains its separate fetch-target policy. When false,
fetch checks destinations and redirects against private networks; if the selected
route uses a proxy, fetch returns a configuration error because it cannot verify
the proxy's upstream peer. Use `direct`, bypass that destination with `NO_PROXY`,
or explicitly allow private targets. A local proxy address is valid in the default
mode, where `allow_private_networks` is true. This target restriction is specific
to fetch; search calls the fixed hosted provider endpoints.
