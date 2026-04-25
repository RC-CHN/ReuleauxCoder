# AGENTS.md — ReuleauxCoder

## Build & Run

本项目使用 **uv** 管理 Python 依赖和虚拟环境。

```bash
# 创建 venv 并安装所有依赖（含 dev）
uv sync

# 运行测试
uv run pytest -v

# 单独跑某文件
uv run pytest tests/domain/agent/test_tool_execution.py -v

# 运行 CLI
uv run rcoder
```

## Project Structure

```
reuleauxcoder/
├── domain/          # 核心领域：Agent, AgentLoop, ToolExecutor, Config, Context
│   └── agent/       # loop.py, tool_execution.py, agent.py
├── extensions/      # 工具扩展
│   └── tools/       # shell, read, write, edit, glob, grep, agent (sub-agent)
├── interfaces/      # 入口层：CLI, TUI, entrypoint/runner
├── infrastructure/  # 平台信息、持久化
└── services/        # LLM 客户端、prompt 构建、config 加载
```

## Key Design Notes

- **Shell tool CWD**: `ShellTool._cwd` 跟踪 cd 操作，`ToolExecutor` 在每次 shell 执行后将 `_cwd` 同步到 `agent.runtime_working_directory`，最终反映在 Runtime Context 中（loop.py `_runtime_tail_message`）。
- **Prompt caching**: System prompt 分 STATIC / SEMI_STATIC 两个 zone，动态信息放在末尾的 Runtime Context。
- **Tool backends**: 支持 local 和 remote_relay 两种后端，通过 `@backend_handler` 装饰器分发。
