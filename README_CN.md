# ReuleauxCoder

> Reinventing the wheel, but only for those who prefer it non-circular.

终端原生 AI 编程助手。

灵感来自并作为 [CoreCoder](https://github.com/he-yufeng/CoreCoder) 的完整重写而启动。

[English](README.md)

## 安装

### 全局安装（推荐）

先安装 [`pipx`](https://pipx.pypa.io/stable/how-to/install-pipx/)，再用 release 中的 wheel 进行全局安装：

```bash
pipx install https://github.com/RC-CHN/ReuleauxCoder/releases/download/v0.3.3/reuleauxcoder-0.3.3-py3-none-any.whl
```

或者使用 [`uv`](https://docs.astral.sh/uv/)（v0.4.0+）：

```bash
uv tool install https://github.com/RC-CHN/ReuleauxCoder/releases/download/v0.3.3/reuleauxcoder-0.3.3-py3-none-any.whl
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
| TypeScript / JavaScript | `typescript-language-server` (npx) | npx 自动安装 |
| YAML | `yaml-language-server` (npx) | npx 自动安装 |
| Bash | `bash-language-server` (npx) + `shellcheck` | `apt install shellcheck` |
| Go | `gopls` | `go install golang.org/x/tools/gopls@latest` |
| C / C++ | `clangd` | `apt install clangd` |
| Rust | `rust-analyzer` | `rustup component add rust-analyzer` |

基于 npx 的服务器（Python、TS/JS、YAML、Bash）会在首次使用时通过 `npx -y` 自动安装。Go、C/C++、Rust 需要单独安装。

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
/model <profile>  切换到指定模型配置
/skills           查看已发现的 skills
/skills reload    重新扫描 skills
/skills enable <n>  启用一个 skill
/skills disable <n> 禁用一个 skill
/tokens           显示 token 使用量
/compact          压缩当前对话上下文
/save             保存会话到磁盘
/sessions         列出已保存会话
/session <id>     在当前进程中恢复指定会话
/session latest   恢复最近一次保存的会话
/approval show    显示审批规则
/approval set ... 更新审批规则
/debug on|off     切换 LLM 调试追踪
/mcp show         显示 MCP 服务器状态
/mcp enable <s>   启用一个 MCP 服务器
/mcp disable <s>  禁用一个 MCP 服务器
/thinking         查看上轮推理内容
/thinking inline  切换推理内容的内联流式显示
/thinking effort  查看当前思考预算
/thinking effort <low|medium|high>  设置思考预算（会话级）
/quit             退出
/exit             退出
```

输错的斜杠命令（如 `/thiking`）会通过编辑距离（≤2）模糊匹配并建议正确的命令。

### 命令说明

- `/reset` 只会清空当前内存中的对话，不会删除已保存的会话。
- `/new` 会先自动保存上一段对话，再开启一段新的对话。
- `/model` 会列出 `config.yaml` 中配置的模型档案；`/model <profile>` 会切换并持久化当前激活档案。
- `/skills` 会展示当前发现的 skills；`/skills reload` 会重新扫描工作区和用户目录；`/skills enable|disable <name>` 会把状态持久化到工作区配置。
- `/session <id>` 会在当前进程中恢复会话；也可以用 `rcoder -r <id>` 在启动时直接恢复。
- `/approval set` 当前支持的目标格式包括 `tool:<name>`、`mcp`、`mcp:<server>`、`mcp:<server>:<tool>`；动作支持 `allow`、`warn`、`require_approval`、`deny`。
- `/mcp enable <server>` 与 `/mcp disable <server>` 会更新工作区配置，并尝试在运行时立即生效。
- `/thinking` 以灰色面板展示模型上一轮的链式推理内容。`/thinking inline` 切换安静模式（仅显示 `Thinking...` 标记）和内联模式（灰色流式输出）。`/thinking effort` 查看或设置思考预算（low/medium/high），支持按 profile 配置自定义值映射。

## CLI 参数

```bash
rcoder [-c CONFIG] [-m MODEL] [-p PROMPT] [-r ID]
```

- `-c, --config`：指定 `config.yaml` 路径
- `-m, --model`：覆盖配置中的模型
- `-p, --prompt`：单次提问模式（非交互）
- `-r, --resume`：按会话 ID 恢复已保存会话
- `-v, --version`：显示版本号

## 许可证

AGPL-3.0-or-later
