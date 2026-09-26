# 配置操作与排障

## 检查同一份配置

以下 `<核心Python>`、`<工作区>`、`<显式文件>` 来自 skill catalog 的 `<skill_runtime>`，不是需要用户填写的固定字符串。执行命令时逐个引用参数：POSIX 可以用 `'/path with spaces/python'`，PowerShell 使用 `& 'C:\path with spaces\python.exe'`。不要把展示用的 argv JSON 当 shell 字符串执行。

```text
<核心Python> -m reuleauxcoder config describe --workspace <工作区>
<核心Python> -m reuleauxcoder config describe --section models --workspace <工作区>
<核心Python> -m reuleauxcoder config inspect --workspace <工作区>
<核心Python> -m reuleauxcoder config check --workspace <工作区>
```

启动时指定了 config，四种命令都追加 `--config <显式文件>`。始终保留相同工作区，不能因为刚刚 `cd` 到子目录，就开始检查另一份配置。没有运行时提示时先确认实际启动器/解释器，不依赖 PATH 的旧 rcoder。

- `describe` 不需要有效配置，返回 API 版本、schema、能力和约束。
- `inspect` 返回 `sources`、`revision`、`valid`、`diagnostics`、`next_start`、`model_targets`。独立命令的 `runtime` 为 null；不能据此声称已读取运行中会话参数。
- 默认 `check` 做 static + startup；后者在隔离进程构造 provider 客户端，不启动 Agent/MCP/LSP。`valid` 表示静态配置有效，仍需逐项看 `checks[].status`。
- `check --content - --scope workspace` 从 stdin 检查该层原始 YAML，不写文件；`--scope user` 检查全局层，`explicit` 要先绑定显式文件。内容是完整目标文档，不是局部补丁，也不是 JSON 包装。多缓冲区用 RPC `config.check` 的 `documents: [{scope, content}, ...]`，最多三份，每份最多 1 MiB。
- `check --revision <inspect.revision>` 可检测所有来源文件是否变更，不能替代写文件时的冲突检查。重新读取、合并自己的改动并再检查，不覆盖新内容。
- `check --check model --profile <名字>` 才会访问模型；可重复 profile，最多八个，每个探测最多 20 秒。返回失败/unknown 时保留错误阶段，分清结构、客户端构造和网络/认证失败。

## 示例基础与继承

下面各例是**局部配置片段**，应合入目标文件而不是替换全文。示例模型 ID 和密钥都用于离线说明，不能直接拿去联网；使用用户实际的模型、端点、凭证和容量。基础片段可位于用户配置中：

<!-- example: base -->
```yaml
models:
  active_main: main
  active_sub: main
  profiles:
    main:
      provider: openai-compatible
      request_mode: chat-completions
      model: example-model
      api_key: example-key-not-a-real-credential
      max_context_tokens: 128000
```

普通配置字典递归合并、列表整体替换。例如用户层有 main 的凭证，工作区只需覆盖 main 的 effort，不复制密钥；profile 缺省字段先继承 `app`。用户层某字段已覆盖内置默认时，删掉工作区字段会回到用户值，而非内置值。删除一个工作区 profile 也不删除用户层同名 profile。

### 只隐藏人类界面的推理

<!-- example: hidden-ui -->
```yaml
ui:
  reasoning_display: hidden
```

保留模型 thinking、effort 与 preserve。如果用户要关闭模型推理，先核对供应商支持的关闭参数，不能拿这个 UI 选项代替。

### 为一个 profile 提高推理强度

确认端点接受 `high` 后，工作区覆盖：

<!-- example: effort -->
```yaml
models:
  profiles:
    main:
      reasoning_effort: high
      thinking_enabled: null
```

不要顺便改 `app` 影响所有模型。自定义档位映射仅在端点确实支持其 API 值时使用；当前 profile 的 UI 档位名称可以不同于 API 值。

### 兼容接口需要 thinking 与工具轮 reasoning 字段

仅适用于已核实支持此格式的 Chat Completions 端点。显式清除继承的 effort，防止它遮蔽 thinking；补位不能修复缺失的真实签名/推理状态。

<!-- example: thinking-replay -->
```yaml
models:
  profiles:
    main:
      reasoning_effort: null
      thinking_enabled: true
      preserve_reasoning_content: true
      reasoning_replay_mode: tool_calls
      reasoning_replay_placeholder: "[PLACE_HOLDER]"
```

### 切换到 Responses

仅当目标端点、模型支持该协议及当前 rcoder 的缓存/加密请求字段时使用。不能保留从 app 继承的 thinking 布尔值。

<!-- example: responses -->
```yaml
models:
  profiles:
    main:
      request_mode: responses
      reasoning_effort: high
      thinking_enabled: null
      responses:
        state: local
        cache:
          mode: implicit
```

### 为一个模型保留全局上下文策略，只关闭自动摘要

<!-- example: context -->
```yaml
models:
  profiles:
    main:
      context:
        auto_snip: null
        auto_summarize: false
        auto_collapse: null
```

null 在这里继承全局同名开关；不是关闭所有压缩。关闭自动摘要可能减少摘要调用，但也可能让会话更早达到容量；按实际目标解释取舍。

### 按名禁用内置 skill

<!-- example: disabled-skill -->
```yaml
skills:
  disabled:
    - rcoder-config
```

已有 disabled 列表时保留其他条目。它禁用的是最终解析后的同名 skill，包含工作区/用户覆盖版本。直接改 YAML 下次启动生效；现有 `/skills disable rcoder-config` 是可以立即更新目录的会话操作，并持久化工作区禁用状态。`/skills reload` 会重读 SKILL.md，但不读取刚编辑的配置开关。

## 工具调用审批

先从 inspect 查看当前合并后的 `approval.rules`、`default_mode`、`reviewer`；确认用户指的是哪些工具/资源及哪个配置范围。规则按具体程度而非简单列表顺序匹配，多个资源取最严格结果。

`approval.rules` 为替换式列表。增加一条规则时必须保留用户要继续生效的原有规则；不能只写一条 allow 规则却无意移除内置/全局规则。`rules: []` 也会清空继承的列表，不是恢复默认。工具名为 `shell`；`pattern` 支持精确资源、`目录/**`、`*`，不是通用正则或 shell 命令解析器。

工具是否暴露由 modes 等机制决定，审批是在调用时做的授权判断。`allow` 不等于工具自动出现在模型列表。自动审批还需要明确的 reviewer profile；基础模型连通测试不证明审批输出格式、工具权限或实际行为通过。

## MCP、LSP 与技能安装

- MCP 使用 `mcp.servers.<name>` 的 stdio `command` 和 argv `args`；环境写 `env`，进程目录写 `cwd`。程序运行在核心主机，VS Code Remote 时通常要安装在远端。不要复制其他客户端的 `mcpServers`、HTTP/SSE URL 字段。
- LSP 使用内置语言名和 `lsp.servers.<language>` 覆盖，不能添加自创语言名。检查配置有效后，还要用现有 LSP 状态/诊断验证进程可启动与文件实际匹配。
- 新 skill 放在工作区或用户 `.rcoder/skills/<name>/SKILL.md`，正文引用的资源使用相对路径。不要编辑安装包中的预置原件；需要定制时放同名用户/工作区版本，加载顺序为内置 → 用户 → 工作区。普通用户文件读取/写入继续适用现有权限。
- 新建/修改 skill 文件后可 `/skills reload` 刷新当前进程，不必为此实现或调用 config 热重载。

## 常见问题

| 现象 | 先检查什么 |
| --- | --- |
| 保存了但仍是旧值 | 下次启动配置与会话运行时不同；确认已保存、实际主机/工作区、显式启动层、app/profile 继承和会话覆盖 |
| API 命令找不到 | 是否误用 PATH 上旧核心；查看启动提示中的 Python 与 API 版本 |
| 改全局后本项目没变化 | 工作区或显式文件仍覆盖同一字段；删除相应覆盖项才能继承 |
| 校验通过但模型报错 | valid 只表示结构有效；检查 startup/model 的实际结果及协议参数支持 |
| thinking false 仍在思考 | 非空 effort 优先；null 使用上游默认；UI 有文字也不直接代表本次计算强度 |
| 没有推理文字 | 上游是否返回对应 delta、preserve 是否开启、UI 是否隐藏；Responses 加密项不是可读文本 |
| MCP/LSP 校验通过却不可用 | 主机上可执行文件、依赖、cwd/env、启动与握手；config check 不执行这些服务 |
| 配置被破坏无法启动 | 用同一核心的独立 describe/inspect/check 定位；恢复本次改动或用户选定的文件历史，再静态/启动检查；不需要运行中的 Agent |
| YAML 报未知字段/重复键 | 按 describe 修正；顶层必须是对象，不接受重复/非字符串键、任意 Python 对象或超过限制的文档 |

汇报只列本次真实检查和关键结果；说明待重启/待外部验证的内容，不粘贴完整配置或原始敏感日志。
