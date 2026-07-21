# ReuleauxCoder

> Reinventing the wheel, but only for those who prefer it non-circular.

终端原生 AI 编程助手，提供 FORGE 风格 CLI、隔离的 subagent、审批、会话、
MCP、skills、LSP 与轻量远端执行 peer。

v0.5.0 CLI 使用 prompt_toolkit mini-TUI，提供常驻执行面板、可滚动 transcript
和集中审批/输入区域。它与未来完整 TUI 共用框架无关的 presentation state；
当前仍未发布 Textual 完整 TUI。

灵感来自并作为 [CoreCoder](https://github.com/he-yufeng/CoreCoder) 的完整重写而启动。

[English](README.md)

## 安装

### 全局安装（推荐）

先安装 [`pipx`](https://pipx.pypa.io/stable/how-to/install-pipx/)，再用 release 中的 wheel 进行全局安装：

```bash
pipx install https://github.com/RC-CHN/ReuleauxCoder/releases/download/v0.5.0/reuleauxcoder-0.5.0-py3-none-any.whl
```

或者使用 [`uv`](https://docs.astral.sh/uv/)（v0.5.0+）：

```bash
uv tool install https://github.com/RC-CHN/ReuleauxCoder/releases/download/v0.5.0/reuleauxcoder-0.5.0-py3-none-any.whl
```

安装完成后，`rcoder` 命令在任意目录下都可以直接使用：

```bash
rcoder --version
rcoder
```

### 从源码运行（面向开发者）

`uv run rcoder` 仅在项目目录内有效，适合开发调试，不建议终端用户使用。

```bash
uv sync
uv run rcoder
```

## 快速开始

首次运行时，`rcoder` 会自动在 `~/.rcoder/config.yaml` 生成全局配置模板。编辑该文件，填入你的 API 凭据：

```bash
rcoder
# → 已生成 ~/.rcoder/config.yaml，请编辑它并填入 API key 和模型。
```

编辑 `~/.rcoder/config.yaml` 中的 API key 后，再次运行：

```bash
rcoder
```

### 项目级配置（可选）

如需在某个项目中使用不同的模型、自定义 MCP 服务器或审批规则，可以在项目根目录下创建 `.rcoder/config.yaml`。该文件会与全局配置合并，完全可选。

```bash
# 仅在需要项目级覆盖时使用
mkdir -p .rcoder
cp config.yaml.example .rcoder/config.yaml   # 或自行编写
```

## 远端 Bootstrap（Host/Peer）

先在 A 机的 `.rcoder/config.yaml` 中配置 remote relay：

```yaml
remote_exec:
  enabled: true
  host_mode: true
  relay_bind: 127.0.0.1:8765
  bootstrap_access_secret: <长随机字符串>
  bootstrap_token_ttl_sec: 120
  peer_token_ttl_sec: 3600
```

然后用下面命令启动 host 模式：

```bash
rcoder --server
```

> 注意：`--server` 仍然是必须的。它会开启 server mode，但 relay 实际监听地址会严格按 `relay_bind` 配置生效。

之后可以在 B 机通过一条命令拉起 peer：

```bash
RC_HOST="https://<HOST>" \
RC_BOOTSTRAP_SECRET='<你的 bootstrap secret>' \
sh -c 'curl -fsSL -H "X-RC-Bootstrap-Secret: ${RC_BOOTSTRAP_SECRET}" "${RC_HOST}/remote/bootstrap.sh" | sh'
```

服务端会先通过 HTTPS 校验 `Bootstrap Access Secret`，校验通过后才会签发一个短期、一次性的 bootstrap token，并嵌入返回的脚本中。

> 注意：脚本已内置 TTY 兜底处理。即使通过 pipe 执行（`curl | sh`），也会优先尝试从 `/dev/tty` 进入 `--interactive`；若无可用 TTY，则自动降级为非交互模式并保持 peer 在线。

## Language Server Protocol (LSP)

ReuleauxCoder 集成了真实的语言服务器，提供代码智能功能：跳转到定义、查找引用、文件符号列表、保存时诊断。

### 支持的语言

| 语言 | LSP 服务器 | 安装方式 |
|---|---|---|
| Python | `pyright-langserver` (npx) | npx 自动安装 |
| TypeScript / JavaScript | TypeScript 7 原生 LSP；TypeScript 6 legacy adapter | 由 `lsp.typescript_mode` 自动选择 |
| YAML | `yaml-language-server` (npx) | npx 自动安装 |
| Bash | `bash-language-server` (npx) + `shellcheck` | `apt install shellcheck` |
| Go | `gopls` | `go install golang.org/x/tools/gopls@latest` |
| C / C++ | `clangd` | `apt install clangd` |
| Rust | `rust-analyzer` | `rustup component add rust-analyzer` |

基于 npx 的服务器（Python、TS/JS fallback、YAML、Bash）会在首次使用时通过
`npx -y` 安装。TypeScript 模式支持 `auto`、`native`、`legacy`：native 使用
TypeScript 7 的 `tsc --lsp --stdio`，legacy 为 TypeScript 6 工作区使用
`typescript-language-server`。Go、C/C++、Rust 需要单独安装。

### 主动 LSP 工具

`lsp` 工具提供只读的代码智能操作：

- `goToDefinition` — 查找符号的定义位置
- `findReferences` — 查找符号的所有引用
- `documentSymbol` — 列出文件中的所有符号（函数、类、变量等）

所有 LSP 操作均为只读，**无需**审批。

## 命令

```text
/help             显示帮助
/reset            仅清空当前内存中的对话
/new              开启新对话（会自动保存上一段对话）
/model            列出模型配置与当前激活配置
/model <profile>  切换当前会话的主模型配置
/model set-main <profile>  持久化全局主模型配置
/model set-sub <profile>   持久化全局 subagent 模型配置
/mode             查看可用模式
/mode switch <n>  切换当前会话模式
/skills           查看已发现的 skills
/skills reload    重新扫描 skills
/skills enable <n>  启用一个 skill
/skills disable <n> 禁用一个 skill
/tokens           显示 token 使用量
/compact          压缩当前对话上下文
/save             保存会话到磁盘
/session          列出已保存会话（`/session <编号|ID|latest>` 恢复）
/session all      包含所有 fingerprint 的会话
/session <编号|ID|latest>  在当前进程中恢复
/approval show    显示审批规则
/approval set ... 更新审批规则
/debug on|off     切换 LLM 调试追踪
/mcp show         显示 MCP 服务器状态
/mcp enable <s>   启用一个 MCP 服务器
/mcp disable <s>  禁用一个 MCP 服务器
/agents             列出后台 subagent 任务（`/jobs` 为兼容别名）
/agents get <id>    查看一个 subagent 任务
/agents wait <id>   等待一个 subagent 任务
/agents message <id> <文本>  在子 agent 下一安全轮次投递消息
/agents resume <id> <文本>   恢复已完成的子 agent transcript
/agents cancel <id>          请求协作式取消
/agents cleanup <id>         删除保留的隔离 worktree
/config           查看 effective config 与来源
/thinking         查看上轮推理内容
/thinking inline  切换推理内容的内联流式显示
/thinking effort  查看当前思考预算
/thinking effort <low|medium|high>  设置思考预算（会话级）
/quit             退出
```

输错的斜杠命令（如 `/thiking`）会通过编辑距离（≤2）模糊匹配并建议正确的命令。

### 命令说明

- `/reset` 只会清空当前内存中的对话，不会删除已保存的会话。
- `/new` 在 `session.auto_save` 开启时先保存上一段对话，再开启新会话。
- `/model` 展示模型档案和路由；会话级切换不会改写全局默认值，持久化默认值请使用 `/model set-main` 或 `/model set-sub`。
- `/skills` 会展示当前发现的 skills；`/skills reload` 会重新扫描工作区和用户目录；`/skills enable|disable <name>` 会把状态持久化到工作区配置。
- `/session` 按当前 fingerprint 展示最新优先的编号列表，预览取最近一条真实用户请求而不是生命周期标记。恢复可使用编号、完整 ID 或 `latest`；启用 auto-save 时会先保存正要离开的会话，并在 CLI 回放最近三个用户轮次。也可以用 `rcoder -r <id>` 在启动时恢复。
- `/approval set` 当前支持的目标格式包括 `tool:<name>`、`mcp`、`mcp:<server>`、`mcp:<server>:<tool>`；动作支持 `allow`、`warn`、`require_approval`、`deny`。
- `/mcp enable <server>` 与 `/mcp disable <server>` 会更新工作区配置，并尝试在运行时立即生效。
- `/thinking` 展示上一轮保留的推理内容；`/thinking inline` 切换内联流式输出。FORGE 活动行会随 reasoning chunk 推进，并保留在历史中。`/thinking effort` 查看或设置当前会话的思考预算。
- Subagent 使用有界父上下文投影、可崩溃恢复的 typed immediate-parent mailbox、父→子指令审计、awaited/detached 自动续跑、runtime-managed execute→verify 屏障、持久化 transcript/job lifecycle、共享预算、stale 恢复、冲突提示和可选 detached worktree。worker 不会在 tool batch 中途修改父历史；root 运行时的新输入会先写 ledger，再在下一安全边界生效。

交互式 TTY 使用 mini-TUI；one-shot、重定向、server 和远端 peer 保持 append-only。
CLI 将模型上下文截断与人类界面折叠分开处理。Shell 运行时显示最近五行滑动窗口，
完成后历史保留最后五行；超时或取消仍会把部分输出交给模型。write/edit 审批统一使用
带框 diff；等待审批期间磁盘文件发生变化时会刷新预览并重新请求确认。
会话会持久化 append-only JSONL 账本、含 wire settings 的 canonical replay、hook transform
后的精确请求审计、实际 usage、Plan/Progress、validated semantic checkpoint 与工具 artifact；
恢复不会重新生成旧 summary，环境或配置变化只会追加在已提交前缀尾部。

## CLI 参数

```bash
rcoder [-c CONFIG] [-m MODEL] [-p PROMPT] [-r ID] [--server]
```

- `-c, --config`：指定 `config.yaml` 路径
- `-m, --model`：覆盖配置中的模型
- `-p, --prompt`：单次提问模式（非交互）
- `-r, --resume`：按会话 ID 恢复已保存会话
- `--server`：按 `remote_exec.relay_bind` 启动独立远端 relay host
- `-v, --version`：显示版本号

## 开发检查

```bash
uv run ruff check .
uv run pytest -q
(cd reuleauxcoder-agent && go test ./...)
```

项目支持 Python 3.10 及以上版本；当前 CI 使用 Python 3.12。真实 LSP 矩阵使用
单独的 opt-in integration suite。

## 许可证

AGPL-3.0-or-later
