# ReuleauxCoder

> Reinventing the wheel, but only for those who prefer it non-circular.

终端原生 AI 编程助手，提供 FORGE 风格 CLI、隔离的 subagent、审批、会话、
MCP、skills、LSP 与轻量远端执行 peer。

CLI 使用终端原生滚屏、Rich Markdown 输出和 prompt_toolkit 行编辑。
独立 React + Ink TUI 位于 `reuleauxcoder-tui/`；两个前端共用 JSON-RPC
运行时，消息、slash 命令和审批都经过同一协议边界。

网页抓取和搜索默认使用环境变量中的代理；可用 `web.proxy` 指定直连或固定的
HTTP/SOCKS5 代理。详见[网页工具网络配置](docs/web-tools.md)。

灵感来自并作为 [CoreCoder](https://github.com/he-yufeng/CoreCoder) 的完整重写而启动。

[English](README.md)

## VS Code 扩展

[VS Code 扩展](reuleauxcoder-vscode/README.md) 支持本机和 Remote 工作区：右侧会话、中央原生 diff 审批、编辑器上下文与文件/图片粘贴上传。界面默认跟随 VS Code 使用中文或英文，缺少核心时可在工作区主机安装附带的兼容版本。

下载 [reuleauxcoder-0.11.1.vsix](https://github.com/RC-CHN/ReuleauxCoder/releases/download/v0.11.1/reuleauxcoder-0.11.1.vsix)，在 VS Code 中运行 **Extensions: Install from VSIX** 安装；Remote 窗口需安装到对应的工作区主机。

## 安装

### 全局安装（推荐）

先安装 [`pipx`](https://pipx.pypa.io/stable/how-to/install-pipx/)，再用 release 中的 wheel 进行全局安装：

```bash
pipx install https://github.com/RC-CHN/ReuleauxCoder/releases/download/v0.11.1/reuleauxcoder-0.11.1-py3-none-any.whl
```

或者使用 [`uv`](https://docs.astral.sh/uv/)：

```bash
uv tool install https://github.com/RC-CHN/ReuleauxCoder/releases/download/v0.11.1/reuleauxcoder-0.11.1-py3-none-any.whl
```

wheel 内含 TUI 及其 JavaScript 依赖，安装不需要 Node 或 npm。
安装完成后，三个命令在任意目录下都可以直接使用：

```bash
rcoder --version
rcoder       # 交互终端中有 Node >=22 则启动 TUI，否则说明原因并使用 CLI
rcoder-cli   # 显式启动线性 CLI
rcoder-tui   # 显式启动 TUI；Node 缺失、过旧或终端不支持时明确报错
```

`--prompt`、`--server`、`--rpc-stdio` 和输入输出重定向始终走 CLI/后端模式。
TUI 资源缺失会提示重新安装发布 wheel 或从源码构建，不会默默启动残缺界面。
TUI 默认使用 uv tool 自己环境中的 Python 启动后端，不依赖系统 Python。

### 从源码运行（面向开发者）

`uv run rcoder` 仅在项目目录内有效，适合开发调试，不建议终端用户使用。

```bash
uv sync
uv run rcoder-cli
# 需要 TUI 时先构建一次资源（开发环境需要 Node >=22 和 npm）：
npm ci --prefix reuleauxcoder-tui
npm run bundle --prefix reuleauxcoder-tui
uv run rcoder
```

## 快速开始

在核心运行的主机上创建 `~/.rcoder/config.yaml`，填入模型凭据：

```yaml
app:
  api_key: "你的 API key"
  model: "提供方的模型 ID"
```

然后执行 `rcoder config check` 检查配置，再运行 `rcoder`。加载配置不会自动生成或改写文件。仓库中的 `config.yaml.example` 提供更多字段示例。

### 项目级配置（可选）

如需在某个项目中使用不同的模型、自定义 MCP 服务器或审批规则，可以在项目根目录下创建 `.rcoder/config.yaml`。该文件会与全局配置合并，完全可选。

```bash
# 仅在需要项目级覆盖时使用
mkdir -p .rcoder
cp config.yaml.example .rcoder/config.yaml   # 或自行编写
```

可用 `rcoder config inspect` 查看脱敏后的配置及来源，用 `rcoder config check` 在不启动 Agent 的情况下检查配置。[只读配置接口](docs/configuration-api.md) 提供字段说明、分层检查及未保存 YAML 检查；即使正常启动失败也能使用。修改通过编辑器或普通文件工具完成，模型连接测试为可选操作。持久化变更在下次核心启动时生效。

## React TUI

独立的 React + Ink 前端位于 [`reuleauxcoder-tui/`](reuleauxcoder-tui/README.md)，通过 JSON-RPC 使用 Python 运行时，提供一级 slash 菜单、命令面板、审批和固定输入区。安装发布 wheel 后，在具备 Node.js 22+ 的交互终端中运行 `rcoder` 或 `rcoder-tui` 即可启动 TUI。以下为前端开发方式：

```sh
npm --prefix reuleauxcoder-tui ci
npm --prefix reuleauxcoder-tui run build
node reuleauxcoder-tui/dist/cli.js
```

用 `rcoder-tui --cwd /path/to/project` 指定工作区，或用 `rcoder-cli` 显式启动线性 CLI。快捷键、功能对应和 SSH 后端用法见前端 README。

CLI 和 TUI 可直接粘贴本地图片路径，输入框和发出的用户消息中会显示 `[Image #1]` 附件标记；`/attach <路径>` 保留作备用入口。图片模型配置 `support_modal: [text, image]`，默认 `[text]`。普通图片默认压缩到 256 KiB，随历史保留；切到文字模型只改变请求投影，切回后恢复仍符合策略的图片。详见[图片输入与上下文保留](docs/images.md)。

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
/model set-main <profile>  持久化工作区主模型配置
/model set-sub <profile>   持久化工作区 subagent 模型配置
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
- `/model` 展示模型档案和路由；会话级切换不会改写全局默认值，持久化工作区默认值请使用 `/model set-main` 或 `/model set-sub`。
- `/skills` 展示内置、用户和工作区 skills；`/skills reload` 重新扫描这些来源；`/skills enable|disable <name>` 把状态持久化到工作区配置。同名技能优先级为工作区 > 用户 > 内置。
- 核心预置 `rcoder-config`，让模型通过普通文件编辑和只读检查配置自身；[字段参考](docs/configuration-reference.md) 详细说明默认值、继承、推理强度、thinking、返回与回放，以及协议限制。CLI、TUI、VS Code 均无需另行安装，可用 `/skills disable rcoder-config` 禁用。配置文件修改在下次核心启动生效，skills reload 不等于配置热重载。
- 核心共预置 17 个[自包含技能](docs/builtin-skills.md)，覆盖 PPTX、Word、Excel、PDF、技能创建/安装、项目指引、代码审查与精简、测试、GitHub CI、界面验收、文档、翻译、润色和结构化数据。技能说明与参考默认使用英文，回复沿用用户语言。办公库与渲染器按任务需要准备，不作为核心安装依赖。
- `/session` 按当前 fingerprint 展示最新优先的编号列表，预览取最近一条真实用户请求而不是生命周期标记。恢复可使用编号、完整 ID 或 `latest`；启用 auto-save 时会先保存正要离开的会话，并在 CLI 回放最近三个用户轮次。也可以用 `rcoder -r <id>` 在启动时恢复。
- `/approval set` 当前支持的目标格式包括 `tool:<name>`、`mcp`、`mcp:<server>`、`mcp:<server>:<tool>`；动作支持 `allow`、`warn`、`require_approval`、`deny`。
- `/mcp enable <server>` 与 `/mcp disable <server>` 会更新工作区配置，并尝试在运行时立即生效。
- `/thinking` 展示上一轮保留的推理内容；`/thinking inline` 切换内联流式输出。FORGE 活动行会随 reasoning chunk 推进，并保留在历史中。`/thinking effort` 查看或设置当前会话的思考预算。
- Subagent 使用有界父上下文投影、可崩溃恢复的 typed immediate-parent mailbox、父→子指令审计、awaited/detached 自动续跑、runtime-managed execute→verify 屏障、持久化 transcript/job lifecycle、共享预算、stale 恢复、冲突提示和可选 detached worktree。worker 不会在 tool batch 中途修改父历史；root 运行时的新输入会先写 ledger，再在下一安全边界生效。

自动上下文压缩策略可以通过 `context.auto_snip`、`context.auto_summarize` 和
`context.auto_collapse` 分别控制，三者默认均为 `true`。模型配置可以在
`models.profiles.<name>.context` 下覆盖任意开关；未填写的字段继承全局策略。
关闭自动策略不会禁用对应的 `/compact force <strategy>` 手动命令。

CLI 在所有模式下使用终端原生滚屏。执行中仍可输入：追加提示和延后执行的命令
由后端排队；审批临时接管输入，结束后恢复草稿。Tab 补全命令，Alt+Enter 换行。
F2 / Ctrl+O 打印会话、计划、任务、启动和排队输入详情；F4 打印保留的工具参数与
完整结果；`/thinking` 查看思考内容。Ctrl+C 清空草稿或中断执行；有排队提示时，
第一次推进提示，第二次请求停止。空闲时连按两次退出；
Ctrl+D 或 `/quit` 也会保存并退出。工具实时输出直接追加到滚屏，超时或取消仍保留
部分结果。write/edit 审批统一使用
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
