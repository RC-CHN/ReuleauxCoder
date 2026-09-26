---
name: rcoder-config
description: Configure rcoder itself through config.yaml. Use for model/provider setup, reasoning effort and thinking, reasoning display/replay, context budgets, tool approval policies, MCP/LSP, skills, or global/workspace configuration troubleshooting. 用于配置 rcoder 自身、解释配置字段及排查配置不生效；不用于修改其他应用的配置。
---

# 配置 rcoder 自身

配置文件是唯一持久化来源。使用普通文件工具编辑，使用只读 `describe`、`inspect`、`check` 确认结果；不要寻找已移除的专用写配置工具。用用户的语言解释结果。

## 先定位，再修改

1. 从 skill 目录旁的 `<skill_runtime>` 获取当前核心的 `python`、`workspace`、`config`。用该 Python 执行 `-m reuleauxcoder config <operation> --workspace <workspace>`，有 `config` 时始终追加 `--config <config>`。这些是 argv 参数，按当前 shell 正确引用路径，不能直接拼接未转义文本。PATH 上的 `rcoder` 可能是另一个旧版本。缺少启动信息时先核实，不猜虚拟环境、远端主机或显式配置路径。
2. 调用 `describe` 核对 `api_version` 和能力，再 `inspect` 看实际来源、文件路径、覆盖关系和 `model_targets`。本 skill 面向 API v2；旧版本不支持时先说明版本差异，不把新字段强塞给旧核心。命令可在配置损坏时独立运行，不需要启动 Agent。
3. 明确作用范围：用户说“这个项目”时用工作区；“所有项目默认”时用用户配置。沿用已确定的范围；只有目标不明确且影响不同，才简短询问。显式启动文件优先级最高，改低优先级文件可能不生效。全局配置属于核心所在主机，VS Code Remote 通常是远端；rcoder relay 工具主机与核心主机也可能不同，不能用远端工具路径冒充核心路径。
4. 按需阅读下面的参考文件。先理解合并后的有效值，再读取目标 YAML 原文并做最小修改，保留无关字段、注释和用户正在做的改动。删除覆盖项表示恢复下层继承；列表整体替换；`null` 只清除接受空值的字段，不能泛用。
5. 按已有授权完成编辑。新凭证由用户提供或沿用已有配置，不输出凭证、不把 `inspect` 中的 `[configured]` 脱敏占位写回。`inspect.next_start` 是解析后的内部结构，不是可整份保存的 YAML。没有通用环境变量插值。
6. 编辑后对相同工作区、显式文件执行 `check`（默认 static + startup）。未保存文本可用 `--content - --scope workspace|user|explicit` 从 stdin 检查；多份未保存文档走 RPC `check.documents`。需要确认模型连通时才运行 `--check model --profile <name>`；这会发请求并消耗 Token，失败不等于 YAML 无效。若自己的修改造成无效配置，修正本次修改并复查，不丢弃其他人的修改。
7. 报告修改的文件/范围、关键前后差异、实际通过的检查和生效时机。文件修改在下次核心启动生效；先完成检查再安排已获授权的重启，不能让正在服务用户的模型用 shell 杀掉自己来“验证”。VS Code 可用配置页的检查/重启操作。`/skills reload` 只刷新技能目录，不重载 config.yaml；`/model` 等现有会话操作也不等于重读配置文件。

## 按问题加载参考

- [references/configuration.md](references/configuration.md)：完整的 19 个分区、133 个字段模式；类型、默认值、范围、继承、限制。修改任何字段前查对应分区，运行中 `describe` 为准。
- [references/reasoning.md](references/reasoning.md)：推理强度、thinking、返回与保留、UI 显示、工具轮回放、Responses 加密项和协议限制。用户说“思维链”“深度思考”“不显示思考”时必读，先分清要改变哪一层。
- [references/workflows.md](references/workflows.md)：检查命令、分层修改示例、故障排查、审批/MCP/skills 配置注意事项。按场景读取；示例是补丁片段，不能覆盖完整配置。

## 不混淆这些概念

- 提高推理强度 ≠ 返回更多可见推理文本；供应商未返回的隐藏推理不能通过本地开关获取。
- 隐藏推理 UI ≠ 关闭 thinking；关闭可见推理保留 ≠ 删除 Responses 加密回放状态。
- 工具 approval 是是否允许模型调用工具的策略；不是 diff 的样式，也不是模型能力列表。只修改用户要求的工具/资源范围，不顺手放宽其他审批。
- 增大本地 `max_context_tokens` 不会扩大模型真实上下文；减少 UI 预览不减少模型接收的工具输出。
- `check` 不是通用外部服务验收：MCP/LSP、工具调用、图片与实际任务需要各自验证；不要将静态通过描述为“一切可用”。
