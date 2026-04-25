# ReuleauxCoder

> Reinventing the wheel, but only for those who prefer it non-circular.

A terminal-native AI coding agent.

Inspired by and started as a complete rewrite of [CoreCoder](https://github.com/he-yufeng/CoreCoder).

## Install

### Install from GitHub Release (recommended)

Install [`pipx`](https://pipx.pypa.io/stable/installation/) first, then install the release wheel globally:

```bash
pipx install https://github.com/RC-CHN/ReuleauxCoder/releases/download/v0.2.4/reuleauxcoder-0.2.4-py3-none-any.whl
```

After installation, you can run:

```bash
rcoder --version
rcoder
```

### Run from source

```bash
uv sync
```

## Quick Start

```bash
# Copy the example config to the workspace config location
mkdir -p .rcoder
cp config.yaml.example .rcoder/config.yaml

# Edit .rcoder/config.yaml with your API key and model
uv run rcoder
```

## Remote Bootstrap (Host/Peer)

Configure remote relay in `.rcoder/config.yaml` on machine A:

```yaml
remote_exec:
  enabled: true
  host_mode: true
  relay_bind: 127.0.0.1:8765
  bootstrap_access_secret: <long-random-secret>
  bootstrap_token_ttl_sec: 120
  peer_token_ttl_sec: 3600
```

Then start host mode with:

```bash
rcoder --server
```

> Note: `--server` is still required. It enables server mode, but the relay now listens exactly on the configured `relay_bind` address.

After that, you can bootstrap a peer on machine B with:

```bash
RC_HOST="https://<HOST>" \
RC_BOOTSTRAP_SECRET='<your-bootstrap-secret>' \
sh -c 'curl -fsSL -H "X-RC-Bootstrap-Secret: ${RC_BOOTSTRAP_SECRET}" "${RC_HOST}/remote/bootstrap.sh" | sh'
```

The bootstrap access secret is checked over HTTPS before the server issues a short-lived one-time bootstrap token embedded into the returned script.

> Note: the bootstrap script now includes TTY fallback handling. Even when executed via a pipe (`curl | sh`), it will try to attach interactive mode via `/dev/tty`; if no TTY is available, it automatically falls back to non-interactive mode and keeps the peer online.

## Commands

```text
/help             Show help
/reset            Clear current in-memory conversation only
/new              Start a new conversation (auto-save previous)
/model            List model profiles and current active profile
/model <profile>  Switch to a configured model profile
/skills           Show discovered skills
/skills reload    Reload skills from disk
/skills enable <n>  Enable one skill
/skills disable <n> Disable one skill
/tokens           Show token usage
/compact          Compress conversation context
/save             Save session to disk
/sessions         List saved sessions
/session <id>     Resume a saved session in current process
/session latest   Resume the latest saved session
/approval show    Show approval rules
/approval set ... Update approval rules
/mcp show         Show MCP server status
/mcp enable <s>   Enable one MCP server
/mcp disable <s>  Disable one MCP server
/quit             Exit
/exit             Exit
```

### Command Notes

- `/reset` only clears the current in-memory conversation. It does not delete saved sessions.
- `/new` starts a fresh conversation and auto-saves the previous one first.
- `/model` lists configured model profiles from `config.yaml`; `/model <profile>` switches to one and persists the active profile.
- `/skills` shows discovered skills; `/skills reload` rescans workspace/user skill directories; `/skills enable|disable <name>` persists skill state in workspace config.
- `/session <id>` resumes a saved session in the current process; `rcoder -r <id>` resumes directly on startup.
- `/approval set` currently supports targets like `tool:<name>`, `mcp`, `mcp:<server>`, and `mcp:<server>:<tool>` with actions `allow`, `warn`, `require_approval`, or `deny`.
- `/mcp enable <server>` and `/mcp disable <server>` update workspace config and try to apply the change at runtime.

## CLI Options

```bash
rcoder [-c CONFIG] [-m MODEL] [-p PROMPT] [-r ID]
```

- `-c, --config`: path to `config.yaml`
- `-m, --model`: override model from config
- `-p, --prompt`: one-shot prompt mode (non-interactive)
- `-r, --resume`: resume a saved session by ID
- `-v, --version`: show version

## License

AGPL-3.0-or-later

