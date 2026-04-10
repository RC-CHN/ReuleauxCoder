# ReuleauxCoder

> Reinventing the wheel, but only for those who prefer it non-circular.

终端原生 AI 编程助手。

灵感来自并作为 [CoreCoder](https://github.com/he-yufeng/CoreCoder) 的完整重写而启动。

## 安装

```bash
uv sync
```

## 快速开始

```bash
# 复制示例配置
cp config.yaml.example config.yaml

# 在 config.yaml 中填入你的 API key 和模型
uv run rcoder
```

## 命令

```text
/help             显示帮助
/reset            仅清空当前内存中的对话
/new              开启新对话（会自动保存上一段对话）
/model            列出模型配置与当前激活配置
/model <profile>  切换到指定模型配置
/tokens           显示 token 使用量
/compact          压缩当前对话上下文
/save             保存会话到磁盘
/sessions         列出已保存会话
/session <id>     在当前进程中恢复指定会话
/session latest   恢复最近一次保存的会话
/approval show    显示审批规则
/approval set ... 更新审批规则
/mcp show         显示 MCP 服务器状态
/mcp enable <s>   启用一个 MCP 服务器
/mcp disable <s>  禁用一个 MCP 服务器
/quit             退出
/exit             退出
```

### 命令说明

- `/reset` 只会清空当前内存中的对话，不会删除已保存的会话。
- `/new` 会先自动保存上一段对话，再开启一段新的对话。
- `/model` 会列出 `config.yaml` 中配置的模型档案；`/model <profile>` 会切换并持久化当前激活档案。
- `/session <id>` 会在当前进程中恢复会话；也可以用 `rcoder -r <id>` 在启动时直接恢复。
- `/approval set` 当前支持的目标格式包括 `tool:<name>`、`mcp`、`mcp:<server>`、`mcp:<server>:<tool>`；动作支持 `allow`、`warn`、`require_approval`、`deny`。
- `/mcp enable <server>` 与 `/mcp disable <server>` 会更新工作区配置，并尝试在运行时立即生效。

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
