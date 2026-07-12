# ReuleauxCoder

> Reinventing the wheel, but only for those who prefer it non-circular.

A terminal-native AI coding agent with a FORGE-styled CLI, scoped subagents,
approvals, sessions, MCP, skills, LSP, and a thin remote execution peer.

The v0.4.1 CLI uses a prompt_toolkit mini-TUI with a persistent execution panel,
scrollable transcript, and focused approval/input pane. It shares framework-neutral
presentation state with the future full TUI; no production Textual app is shipped yet.

Inspired by and started as a complete rewrite of [CoreCoder](https://github.com/he-yufeng/CoreCoder).

[中文](README_CN.md)

## Install

### Install globally (recommended)

Install [`pipx`](https://pipx.pypa.io/stable/how-to/install-pipx/) first, then install the release wheel globally:

```bash
pipx install https://github.com/RC-CHN/ReuleauxCoder/releases/download/v0.4.0/reuleauxcoder-0.4.0-py3-none-any.whl
```

Or use [`uv`](https://docs.astral.sh/uv/) (v0.4.0+):

```bash
uv tool install https://github.com/RC-CHN/ReuleauxCoder/releases/download/v0.4.0/reuleauxcoder-0.4.0-py3-none-any.whl
```

After installation, the `rcoder` command is available globally — run it from any directory:

```bash
rcoder --version
rcoder
```

### Run from source (for developers)

`uv run rcoder` only works inside the project directory. This is intended for development, not for end users.

```bash
uv sync
uv run rcoder
```

## Quick Start

On first run, `rcoder` auto-generates a global config template at `~/.rcoder/config.yaml`. Edit it with your API credentials:

```bash
rcoder
# → ~/.rcoder/config.yaml created. Edit it with your API key and model.
```

After editing the API key in `~/.rcoder/config.yaml`, run again:

```bash
rcoder
```

### Workspace-level config (optional)

For per-project overrides — such as different models, custom MCP servers, or approval rules — create `.rcoder/config.yaml` in your project root. This file is merged on top of the global config and is entirely optional.

```bash
# Only needed if you want project-specific overrides
mkdir -p .rcoder
cp config.yaml.example .rcoder/config.yaml   # or write your own
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

## Language Server Protocol (LSP)

ReuleauxCoder integrates with real language servers for code intelligence: go-to-definition, find-references, document-symbols, and on-save diagnostics.

### Supported Languages

| Language | LSP Server | Install |
|---|---|---|
| Python | `pyright-langserver` (npx) | auto-installed via npx |
| TypeScript / JavaScript | TypeScript 7 native LSP; TypeScript 6 legacy adapter | auto-selected (`lsp.typescript_mode`) |
| YAML | `yaml-language-server` (npx) | auto-installed via npx |
| Bash | `bash-language-server` (npx) + `shellcheck` | `apt install shellcheck` |
| Go | `gopls` | `go install golang.org/x/tools/gopls@latest` |
| C / C++ | `clangd` | `apt install clangd` |
| Rust | `rust-analyzer` | `rustup component add rust-analyzer` |

npx-based servers (Python, TS/JS fallback, YAML, Bash) are installed on first
use with `npx -y`. TypeScript mode is `auto`, `native`, or `legacy`: native uses
TypeScript 7's `tsc --lsp --stdio`, while legacy uses
`typescript-language-server` for TypeScript 6 workspaces. Go, C/C++, and Rust
servers must be installed separately.

### Active LSP Tools

The `lsp` tool provides read-only code intelligence:

- `goToDefinition` — find where a symbol is defined
- `findReferences` — find all references to a symbol across the codebase
- `documentSymbol` — list all symbols (functions, classes, variables) in a file

All LSP operations are read-only and do **not** require approval.

## Commands

```text
/help              Show help
/reset             Clear current in-memory conversation only
/new               Start a new conversation (auto-save previous)
/model             List model profiles and current active profile
/model <profile>   Switch the session main model profile
/model set-main <profile>  Persist the global main profile
/model set-sub <profile>   Persist the global subagent profile
/mode              Show available modes
/mode switch <n>   Switch the current session mode
/skills            Show discovered skills
/skills reload     Reload skills from disk
/skills enable <n>   Enable one skill
/skills disable <n>  Disable one skill
/tokens            Show token usage
/compact           Compress conversation context
/save              Save session to disk
/session           List saved sessions (`/session <#|id|latest>` restores one)
/session all       Include sessions from every fingerprint
/session <#|id|latest>  Restore in the current process
/approval show     Show approval rules
/approval set ...  Update approval rules
/debug on|off      Toggle LLM debug trace
/mcp show          Show MCP server status
/mcp enable <s>    Enable one MCP server
/mcp disable <s>   Disable one MCP server
/agents              List background subagent jobs (`/jobs` is an alias)
/agents get <id>     Show one subagent job
/agents wait <id>    Wait for one subagent job
/agents message <id> <text>  Send input at the next safe child round
/agents resume <id> <text>   Resume a completed child transcript
/agents cancel <id>          Request cooperative cancellation
/agents cleanup <id>         Remove a retained isolated worktree
/config            Show effective config values and sources
/thinking          Show reasoning content from the last turn
/thinking inline   Toggle inline streaming of reasoning content
/thinking effort   Show current reasoning effort budget
/thinking effort <low|medium|high>  Set reasoning effort (session-scoped)
/quit              Exit
```

Mistyped slash commands (e.g. `/thiking`) are fuzzy-matched and suggest the closest
known command if within edit distance ≤ 2.

### Command Notes

- `/reset` only clears the current in-memory conversation. It does not delete saved sessions.
- `/new` starts a fresh conversation and saves the previous one first when `session.auto_save` is enabled.
- `/model` lists configured profiles and routing. Session switches do not rewrite global defaults; use `/model set-main` or `/model set-sub` for persisted defaults.
- `/skills` shows discovered skills; `/skills reload` rescans workspace/user skill directories; `/skills enable|disable <name>` persists skill state in workspace config.
- `/session` shows a numbered, newest-first list for the current fingerprint. Its preview is the latest real user request, not lifecycle metadata. Restore accepts the displayed number, a full ID, or `latest`; it saves the session being left when auto-save is enabled and replays the latest three user turns in the CLI. `rcoder -r <id>` restores directly on startup.
- `/approval set` currently supports targets like `tool:<name>`, `mcp`, `mcp:<server>`, and `mcp:<server>:<tool>` with actions `allow`, `warn`, `require_approval`, or `deny`.
- `/mcp enable <server>` and `/mcp disable <server>` update workspace config and try to apply the change at runtime.
- `/thinking` shows reasoning content retained from the most recent turn. `/thinking inline` toggles inline streaming; the FORGE activity row advances as reasoning chunks arrive and remains in history. `/thinking effort` views or sets the session reasoning budget.
- Subagents use bounded `minimal`, `recent`, or `full` parent-context projections, typed immediate-parent mailboxes, awaited/detached continuation, persisted transcripts and job lifecycle, shared budgets, resumable follow-ups, stale-on-resume recovery, conflict notices, and optional detached worktree isolation. Worker callbacks never mutate model history mid-tool-batch. While a root turn is running, new bottom-pane input is ledgered and applied at the next safe boundary.

Interactive TTYs use the mini-TUI; one-shot, redirected, server, and remote-peer
paths stay append-only. The CLI keeps model output and human presentation limits
separate: shell output uses a rolling five-line human tail while the agent retains
the full result subject to context policy. Timeout/cancel preserves partial output.
Write/edit approval uses a framed diff and refreshes if the saved file changes.
Sessions persist an append-only JSONL ledger, canonical replay state, exact request
audits, usage observations, Plan/Progress state, checkpoints, and tool artifacts.

## CLI Options

```bash
rcoder [-c CONFIG] [-m MODEL] [-p PROMPT] [-r ID] [--server]
```

- `-c, --config`: path to `config.yaml`
- `-m, --model`: override model from config
- `-p, --prompt`: one-shot prompt mode (non-interactive)
- `-r, --resume`: resume a saved session by ID
- `--server`: run as a dedicated remote relay host using `remote_exec.relay_bind`
- `-v, --version`: show version

## Development checks

```bash
uv run ruff check .
uv run pytest -q
(cd reuleauxcoder-agent && go test ./...)
```

The package supports Python 3.10 and newer; CI currently exercises Python 3.12.
The real LSP matrix has its own opt-in integration suite.

## License

AGPL-3.0-or-later
