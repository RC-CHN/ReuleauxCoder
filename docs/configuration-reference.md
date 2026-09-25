# config.yaml 配置参考

本文对应以文件为配置源的源码行为（Unreleased），用于查阅配置和编写 rcoder 配置 skill。接口调用见 [Configuration inspection API](configuration-api.md)。字段集合应以运行中的 `rcoder config describe --section models`（或对应 `config.describe` RPC） 为准；本文补充 schema 不能表达的加载规则、用途和已知限制。

## 范围与读取规则

当前管理 schema 有 **19 个顶层分区、133 个字段模式**。统计将动态 profile/server 名称归并为 `{name}`，列表元素归并为 `[]`，任意值字典按一个字段统计；`meta` 是开放对象，不代表只有一个可能的键。接口只读，配置由原生编辑器或普通文件工具修改；提供方能力及外部程序可用性仍需要单独验证。

配置来源从低到高为：内置默认值 → 用户 `~/.rcoder/config.yaml` → 工作区 `.rcoder/config.yaml` → 显式 `--config` 文件。普通字典递归合并；同名模型、模式、MCP server 也合并字段；列表整体替换。删除工作区字段会重新露出用户层或默认值，不能理解为全局禁用。`null` 也不是通用的“删除/恢复默认”操作。

配置文件路径属于核心所在主机。VS Code Remote 场景下是远端工作区和远端用户目录。相对路径由对应运行时组件解释，不统一相对 YAML 文件；目录配置优先使用主机上的绝对路径。不存在通用 `${ENV_VAR}` 插值，不能把 `${OPENAI_API_KEY}` 等占位文本当成已读取的密钥。

编辑 YAML 文件产生的持久化变更在**下次核心启动**时生效。现有 `/model`、`/mode` 等会话命令和会话恢复另有运行时状态，不能把“写入成功”“下次启动配置”和“当前会话生效”混为一谈。`inspect.sources` 是原始分层值，`inspect.next_start` 是转换后的 `Config`，两者字段布局不同；后者不能整份作为 YAML 文档写回。

表中默认值指当前源码缺省值，不代表所有模型提供方都支持该值；实际模型名、价格、上下文容量和推理参数需按用户提供的信息或供应商资料确认。`?` 表示字段接受 `null`。通用参数校验只说明本地结构有效。

## 1. models：主模型、子模型与命名配置

建议新增配置统一使用 `models.profiles`，显式指定主/子 profile。所有 profile 都进行基础配置校验，包括未启用的 profile；占位密钥无法证明可连接。

| 路径 | 类型 / 默认值 | 用途与约束 |
| --- | --- | --- |
| `/models/active_main` | string? / 第一个 profile | 主模型 profile 名称；启动与检查均拒绝不存在的引用 |
| `/models/active_sub` | string? / 主 profile | 子 agent 默认 profile，不影响主模型 |
| `/models/active` | string? / 未设置 | 兼容旧名称；仅在没有 `active_main` 时迁移为主选择，不用于新配置 |
| `/models/profiles/{name}/model` | string / `gpt-4o` | 提供方模型 ID，不是本地 profile 名；必须非空 |
| `/models/profiles/{name}/api_key` | string / 空字符串 | 每个 profile 都需要非空密钥；inspect 读取脱敏 |
| `/models/profiles/{name}/provider` | string / `openai-compatible` | `openai-compatible` 或 `anthropic` |
| `/models/profiles/{name}/request_mode` | string? / `null` | `chat-completions`、`responses`、`messages`；省略时按提供方推导 |
| `/models/profiles/{name}/responses/state` | string / `local` | 当前只支持 `local`，本地持有历史，不使用服务端会话链 |
| `/models/profiles/{name}/responses/cache/mode` | string / `implicit` | `implicit` / `explicit`；后者需模型支持，不是通用省钱开关 |
| `/models/profiles/{name}/support_modal` | string[] / `[text]` | 必须含 `text`，可加 `image`；声明能力，不会让纯文本模型获得视觉能力 |
| `/models/profiles/{name}/base_url` | string? / `null` | 提供方端点；校验要求 HTTP(S)，禁止内嵌认证、query、fragment |
| `/models/profiles/{name}/max_tokens` | integer / `4096` | 单次输出上限，必须正数，不是 Goal 累计预算 |
| `/models/profiles/{name}/temperature` | number / `0.0` | 校验范围 `[0, 2]`，提供方可能更严格 |
| `/models/profiles/{name}/max_context_tokens` | integer / `128000` | 本地上下文容量估计，必须正数，不能靠增大它扩展模型真实容量 |
| `/models/profiles/{name}/preserve_reasoning_content` | boolean / `true` | 保留兼容接口返回的 reasoning 内容以供后续轮次使用 |
| `/models/profiles/{name}/backfill_reasoning_content_for_tool_calls` | boolean / `false` | 为缺少 reasoning 内容的 assistant 工具调用消息补位，服务于特定网关兼容 |
| `/models/profiles/{name}/reasoning_effort` | string? / `null` | 推理等级标签；映射后发送给提供方，任意字符串不等于提供方支持 |
| `/models/profiles/{name}/thinking_enabled` | boolean? / `null` | 提供方 thinking 开关；设置了非空 `reasoning_effort` 时优先使用 effort |
| `/models/profiles/{name}/reasoning_replay_mode` | string? / `null` | 仅接受 `null` / `none` / `tool_calls`，其他值在静态检查时拒绝 |
| `/models/profiles/{name}/reasoning_replay_placeholder` | string? / `null` | reasoning 重放补位文本，运行时默认 `[PLACE_HOLDER]` |
| `/models/profiles/{name}/reasoning_effort_values` | object? / `null` | 标签 → API 值映射；缺省使用 low/medium/high 同名映射，值允许 JSON 数据 |
| `/models/profiles/{name}/reasoning_effort_param` | string / `reasoning_effort` | 兼容接口中的 effort 参数名；必须非空，不能覆盖 model/messages/temperature 等保留字段；Responses 模式固定使用 `reasoning_effort` |
| `/models/profiles/{name}/context/auto_snip` | boolean? / `null` | 覆盖全局同名策略，`null`/省略表示继承 |
| `/models/profiles/{name}/context/auto_summarize` | boolean? / `null` | 覆盖全局自动摘要策略 |
| `/models/profiles/{name}/context/auto_collapse` | boolean? / `null` | 覆盖全局旧轮次折叠策略 |

提供方与协议搭配：`openai-compatible` 支持 Chat Completions（默认）和 Responses；`anthropic` 只支持 Messages（默认）。不能只改 provider 而保留不兼容的 request_mode。所有 profile 都会进行静态与启动构造检查，连通性测试由用户按需选择。`model_targets` 列出合并后的配置，主模型连通不能代表其他模型连通；网络测试不会阻止保存文件。

## 2. app：兼容模型参数与诊断开关

`app` 的模型字段是共享默认值。先按文件层级合并，再把 app 默认值与各 profile 递归合并，最后填充类型默认值。profile 省略字段时继承 app；显式 `null` 清除可空字段的继承（例如 request_mode 回到提供方默认协议）。没有 profile 时，旧式 app 配置迁移为 `default` profile。启动、会话切换、子代理与验证使用同一份解析结果。修改 app 默认值会影响所有继承该字段的 profile，包括审批模型；此时模型调用方会被审批模型保护拦截。

| 路径 | 类型 / 缺省行为 | 说明 |
| --- | --- | --- |
| `/app/model` | string / 迁移时 `gpt-4o` | 旧式模型 ID |
| `/app/api_key` | string / 迁移时空 | 旧式凭证；仍受敏感字段限制 |
| `/app/provider` | string / 迁移时 `openai-compatible` | 旧式提供方 |
| `/app/base_url` | string? / `null` | 旧式端点 |
| `/app/max_tokens` | integer / 迁移时 `4096` | 旧式输出上限 |
| `/app/temperature` | number / 迁移时 `0.0` | 旧式温度 |
| `/app/max_context_tokens` | integer / 迁移时 `128000` | 旧式上下文容量 |
| `/app/request_mode` | string? / `null` | 共享请求协议；profile 省略时继承，显式 null 按 provider 推导 |
| `/app/responses/state` | `local` | 共享 Responses 状态策略，profile 可覆盖 |
| `/app/responses/cache/mode` | `implicit` / `explicit` | 共享缓存策略，嵌套字段递归合并 |
| `/app/support_modal` | string[] / `[text]` | 共享模态声明；profile 的列表整体覆盖 |
| `/app/preserve_reasoning_content` | boolean / `true` | 共享 reasoning 保留开关 |
| `/app/backfill_reasoning_content_for_tool_calls` | boolean / `false` | 共享 reasoning 补位开关 |
| `/app/reasoning_effort` | string? / `null` | 共享推理等级；显式 null 清除继承 |
| `/app/thinking_enabled` | boolean? / `null` | 同上 |
| `/app/reasoning_replay_mode` | string? / `null` | 共享重放模式；有效值见 models |
| `/app/reasoning_replay_placeholder` | string? / `null` | 同上 |
| `/app/llm_debug_trace` | boolean / `false` | LLM 调试追踪；按具体排障需求启用，避免把它当成日常必需设置 |

选择单个模型的参数时优先改该 profile；只有明确希望所有继承者一起改变时才修改 app。`inspect.model_targets` 展示解析后的目标和角色，避免把原始 YAML 当成最终模型参数。

## 3. context：模型上下文管理

这里控制发送给模型的历史和预算；`ui` 控制人看到的展示；`tool_output` 控制工具结果的截断与归档。这三者不能互相替代。

| 路径 | 类型 / 默认值 | 用途与约束 |
| --- | --- | --- |
| `/context/auto_snip` | boolean / `true` | 自动裁剪旧工具输出，profile 可覆盖 |
| `/context/auto_summarize` | boolean / `true` | 自动摘要，可能额外调用模型，profile 可覆盖 |
| `/context/auto_collapse` | boolean / `true` | 自动折叠旧轮次，profile 可覆盖 |
| `/context/image_retention` | string / `history` | `history` 保留历史图片；`user_turn` 限定当前用户轮，steering 延续同一轮 |
| `/context/snip_keep_recent_tools` | integer / `2` | 保护最近 agent round 数，不是简单工具调用数；校验要求非负 |
| `/context/snip_threshold_chars` | integer / `1500` | 裁剪字符阈值；非负 |
| `/context/snip_min_lines` | integer / `6` | 裁剪行数条件；非负 |
| `/context/summarize_keep_recent_turns` | integer / `5` | 摘要时保留最近轮次；非负 |
| `/context/token_fudge_factor` | number / `1.1` | token 估算修正系数；必须大于零 |
| `/context/reserved_output_tokens` | integer / `8192` | 输出预算预留，非负；传入运行时上下文预算 |
| `/context/fixed_prompt_tokens` | integer / `0` | 固定提示词预算预留，非负 |
| `/context/tool_schema_tokens` | integer / `0` | 工具 schema 预算预留，非负 |
| `/context/safety_margin_tokens` | integer / `2048` | 安全余量，非负 |

上述四项预算从 YAML 传入运行时；调大预留量会减少可供会话使用的预算，不会增加模型真实上下文容量。

## 4. ui：人类界面的输出策略

| 路径 | 类型 / 默认值 | 用途与约束 |
| --- | --- | --- |
| `/ui/verbosity` | string / `compact` | `compact` / `standard` / `debug` |
| `/ui/tool_output` | string / `summary` | `errors` / `summary` / `preview` / `full` |
| `/ui/max_preview_lines` | integer / `20` | 人类预览行数上限，正数 |
| `/ui/max_preview_chars` | integer / `1200` | 人类预览字符上限，正数 |
| `/ui/show_tool_args` | boolean / `true` | 展示工具参数 |
| `/ui/reasoning_display` | string / `indicator` | `hidden` / `indicator` / `inline`；不是模型 thinking 能力开关 |
| `/ui/notification_threshold` | string / `info` | `debug` / `info` / `warning` / `error` |

这些是核心的展示策略，具体前端仍有自己的渲染规则；VS Code 主题、字体、扩展安装目录等不属于此 YAML。

## 5. tool_output：工具结果截断与归档

| 路径 | 类型 / 默认值 | 用途与约束 |
| --- | --- | --- |
| `/tool_output/max_chars` | integer / `12000` | 工具结果保留字符上限，正数，影响模型收到的内容 |
| `/tool_output/max_lines` | integer / `120` | 工具结果保留行数上限，正数 |
| `/tool_output/store_full_output` | boolean / `true` | 归档被截断的完整结果以供后续读取 |
| `/tool_output/store_dir` | string? / `null` | 默认 `.rcoder/tool-outputs`，有用户目录回退；当前会话归档还与 session 存储关联 |

截断方式由工具保留策略决定，可能保留头部、尾部或两者。不要通过缩小 `ui` 预览来声称已减少模型 token。

## 6. attachments.image：图片处理与存储限制

全部为正整数，单位见表。普通文件上传上限不是这组字段。

| 路径 | 默认值 | 用途 |
| --- | --- | --- |
| `/attachments/image/max_edge_px` | `2000` | 图像长边像素上限 |
| `/attachments/image/normal_max_bytes` | `262144`（256 KiB） | 普通图片变体的字节预算 |
| `/attachments/image/detail_max_base64_bytes` | `2097152`（2 MiB） | 细节变体 Base64 大小预算 |
| `/attachments/image/originals_cache_max_bytes` | `1073741824`（1 GiB） | 原图缓存预算 |
| `/attachments/image/import_max_bytes` | `67108864`（64 MiB） | 单图导入字节上限 |
| `/attachments/image/max_pixels` | `40000000` | 解码像素上限 |

图片能否进入模型还取决于目标 profile 的 `support_modal` 和 `context.image_retention`。

## 7. skills：发现与启停

| 路径 | 类型 / 默认值 | 用途 |
| --- | --- | --- |
| `/skills/enabled` | boolean / `true` | 整体 skill 发现与可用目录开关 |
| `/skills/scan_project` | boolean / `true` | 扫描工作区 `.rcoder/skills/*/SKILL.md` |
| `/skills/scan_user` | boolean / `true` | 扫描核心主机用户目录 `~/.rcoder/skills/*/SKILL.md` |
| `/skills/disabled` | string[] / `[]` | 按解析后的 skill 名禁用；列表整体替换 |

用户目录先扫描，项目同名 skill 后扫描并覆盖。当前没有 `skills.paths`、远程 skill URL、marketplace 或 builtin skill 包目录配置。`SKILL.md` 要有 YAML frontmatter 的 `name` 和 `description`，正文按 skill 机制读取。`agents/openai.yaml` 不是 rcoder 当前 parser 的输入，不应直接把 Codex 安装结构当成 rcoder 实现。

空目录提示与实际发现路径一致，均使用 `.rcoder/skills/`。

## 8. prompt：附加用户指令

| 路径 | 类型 / 默认值 | 用途 |
| --- | --- | --- |
| `/prompt/system_append` | string / `""` | 添加用户/工作区指令，由提示词组装器放入对应指令块；不替换核心提示词 |

skill 只应按用户所需改变这部分行为，避免把配置任务本身写成永久指令。项目说明文件、skill 正文与该字段是不同机制。

## 9. mcp：本机 stdio 服务

| 路径 | 类型 / 默认值 | 用途与约束 |
| --- | --- | --- |
| `/mcp/servers/{name}/command` | string / `""` | 可执行文件名或路径，不是整段 shell 命令；启用时必须非空 |
| `/mcp/servers/{name}/args` | string[] / `[]` | 逐项 argv，读取整体脱敏 |
| `/mcp/servers/{name}/env` | string→string object / `{}` | 覆盖进程环境，读取整体脱敏 |
| `/mcp/servers/{name}/cwd` | string? / `null` | 子进程工作目录，建议主机绝对路径 |
| `/mcp/servers/{name}/enabled` | boolean / `true` | 启停此 server |

当前 YAML 没有 `url`、`transport`、`headers` 或 SSE/HTTP server 配置字段；不能照搬其他客户端的 `mcpServers` 配置。MCP client 会查找 command、合并环境后启动 stdio 进程。配置校验不会安装依赖、启动 server 或完成 MCP 握手。

## 10. lsp：语言服务器与诊断

| 路径 | 类型 / 默认值 | 用途与约束 |
| --- | --- | --- |
| `/lsp/enabled` | boolean / `true` | LSP 整体开关 |
| `/lsp/poll_timeout_ms` | integer / `5000` | 请求/诊断轮询超时，正数，毫秒 |
| `/lsp/edit_wait_timeout_ms` | integer / `1000` | 编辑后等待诊断，非负；`0` 仅取已就绪结果 |
| `/lsp/max_diagnostics` | integer / `20` | 一次诊断投影的数量限制，正数 |
| `/lsp/max_injection_chars` | integer / `12000` | 诊断注入总字符预算，至少 `512` |
| `/lsp/max_message_chars` | integer / `1000` | 单条诊断文本上限，正数 |
| `/lsp/include_warnings` | boolean / `true` | 是否包含 warning 诊断 |
| `/lsp/typescript_mode` | string / `auto` | `auto` / `native` / `legacy`，选择 TS7 native 或传统 language-server 链路 |
| `/lsp/servers/{name}/cmd` | string? / `null` | 覆盖内置语言启动命令 |
| `/lsp/servers/{name}/args` | string[]? / `null` | 覆盖 argv；`null` 继承，`[]` 清空 |
| `/lsp/servers/{name}/workspace_root` | string? / `null` | 覆盖根目录检测 |
| `/lsp/servers/{name}/init_opts` | object? / `null` | 初始化选项；依赖具体语言服务器 |

实际支持的 `{name}`：`python`、`rust`、`go`、`typescript`、`javascript`、`c`、`cpp`、`bash`、`yaml`。未知语言名会被静态校验拒绝。配置校验不会验证程序已安装或可初始化；`init_opts` 也没有各厂商的完整 schema。

## 11. web：搜索与抓取

| 路径 | 类型 / 默认值 | 用途与约束 |
| --- | --- | --- |
| `/web/enabled` | boolean / `true` | Web 工具注册开关 |
| `/web/proxy` | string / `env` | `env`、`direct` 或 HTTP/HTTPS/SOCKS5/SOCKS5H 代理 URL；不是 LLM/MCP 全局代理开关 |
| `/web/search_provider` | string / `auto` | `auto` / `exa` / `parallel` |
| `/web/allow_private_networks` | boolean / `true` | 是否允许访问私网目标，涉及网络访问边界 |

搜索 API key 由 `EXA_API_KEY` / `PARALLEL_API_KEY` 环境变量读取，不存在 `web.api_key` 字段。代理 URL 可以包含认证信息，inspect 读取会脱敏。普通文件读取可能包含凭证，操作和汇报时应避免泄露。

## 12. shell：RTK 提示

| 路径 | 类型 / 默认值 | 用途 |
| --- | --- | --- |
| `/shell/rtk` | string / `off` | `auto` / `on` / `off`；当前只影响 RTK 可用性/安装提示，**不会自动改写 shell 命令** |

默认 shell 的会话选择走现有 `/shell`，此处没有 executable、args、timeout 配置。远程工具超时见 `remote_exec`。

## 13. session、14. cli、15. goal

| 路径 | 类型 / 默认值 | 用途与约束 |
| --- | --- | --- |
| `/session/auto_save` | boolean / `true` | 现有会话自动保存策略开关；不是对所有持久化路径的删除/禁写开关 |
| `/session/dir` | string? / `null` | 会话根目录；默认工作区 `.rcoder/sessions`，无法创建时回退用户目录 |
| `/cli/history_file` | string? / `null` | 线性 CLI 输入历史，默认 `.rcoder/history`；与模型对话历史不同 |
| `/goal/default_token_budget` | integer? / `null` | 新 Goal 的默认累计预算；必须正整数或 `null`（不限），计入 input − cached input + output |

CLI history 和部分归档路径会展开 `~`，session 入口等并不统一处理。因此不能推广为“所有目录字段支持 `~`”；主机绝对路径更明确。

## 16. modes：工具集合与模式提示词

这组设置通过编辑文件修改，使用现有文件工具审批规则。工具暴露与工具审批是不同步骤。

| 路径 | 类型 / 默认值 | 用途与约束 |
| --- | --- | --- |
| `/modes/active` | string? / `coder` | 默认主模式；引用必须存在，内置 coder/planner/debugger 预先合并 |
| `/modes/profiles/{name}/description` | string / `""` | 模式说明 |
| `/modes/profiles/{name}/tools` | string[] / `[]` | 允许暴露的工具集合，内置 coder 为 `["*"]`；同名内置模式按字段继承后覆盖 |
| `/modes/profiles/{name}/prompt_append` | string / `""` | 模式附加指令 |
| `/modes/profiles/{name}/allowed_subagent_modes` | string[] / `[]` | 允许委派的子 agent 模式；内置使用 explore/execute/verify，不能等同于主 modes profile 列表 |

旧工具名称 `bash` 在模式和审批规则内兼容迁移为 `shell`；新配置使用 `shell`。

## 17. approval：工具调用策略

这组设置通过编辑文件修改。默认模式为 `require_approval`，但省略 rules 时还有内置规则，并不表示每个工具都会弹窗。显式 rules 列表替换内置/低优先级列表；`[]` 也有含义，不会自动补齐默认规则。

| 路径 | 类型 / 默认值 | 用途与约束 |
| --- | --- | --- |
| `/approval/default_mode` | string / `require_approval` | `allow` / `warn` / `require_approval` / `deny`；规则未匹配时使用，内部只读/控制工具另有 allow 回退 |
| `/approval/reviewer` | string / `user` | `user` / `auto_review` |
| `/approval/auto_review_model_profile` | string? / `null` | auto_review 必须指定有效 profile，不静默使用主/子模型代替 |
| `/approval/auto_review_policy` | string / `""` | 自动审批模型使用的附加审批政策 |
| `/approval/auto_review_timeout_seconds` | integer / `15` | 自动审批超时，必须正数，秒 |
| `/approval/rules/[]/tool_name` | string? / `null` | 精确匹配工具名 |
| `/approval/rules/[]/tool_source` | string? / `null` | 匹配来源，运行时 builtin/mcp/unknown |
| `/approval/rules/[]/mcp_server` | string? / `null` | 匹配 MCP server 名 |
| `/approval/rules/[]/effect_class` | string? / `null` | 匹配工具效果类别 |
| `/approval/rules/[]/profile` | string? / `null` | 匹配运行时审批上下文的 profile 维度，不应自行假定是模型 ID |
| `/approval/rules/[]/pattern` | string? / `null` | 匹配稳定资源标识；支持全量 `*`、`目录/**`、精确值，不支持通用 glob/regex |
| `/approval/rules/[]/scope_key` | string? / `null` | 精确匹配运行时范围键，常用于作用域约束 |
| `/approval/rules/[]/action` | string / `require_approval` | 四种 action 同 default_mode |

引擎先按规则具体程度排序，同分保留列表顺序；多个资源分别匹配，再取最严格动作。不能声称“所有规则从上到下第一条命中”。完整默认规则见 `domain/config/schema.py`。

## 18. remote_exec：rcoder relay 主机

这组设置属于 rcoder 自有远程执行协议，不是 VS Code Remote SSH 的安装/连接设置。这组设置通过编辑文件修改。

| 路径 | 类型 / 默认值 | 用途 |
| --- | --- | --- |
| `/remote_exec/enabled` | boolean / `false` | 启用 remote relay 能力 |
| `/remote_exec/host_mode` | boolean / `false` | 核心作为 relay host；`--server` 也会启用 host 模式 |
| `/remote_exec/relay_bind` | string / `127.0.0.1:8765` | relay 监听地址 |
| `/remote_exec/bootstrap_access_secret` | string / `""` | bootstrap 访问秘密，读取脱敏 |
| `/remote_exec/bootstrap_token_ttl_sec` | integer / `300` | bootstrap token 有效期，秒 |
| `/remote_exec/peer_token_ttl_sec` | integer / `3600` | peer token 有效期，秒 |
| `/remote_exec/heartbeat_interval_sec` | integer / `10` | 心跳间隔，秒 |
| `/remote_exec/heartbeat_timeout_sec` | integer / `30` | 心跳失联超时，秒 |
| `/remote_exec/default_tool_timeout_sec` | integer / `30` | 普通远程工具超时，秒 |
| `/remote_exec/shell_timeout_sec` | integer / `120` | 远程 shell 超时，秒 |

监听地址要求 host:port 或 [IPv6]:port，端口范围 0–65535（0 允许系统分配）；TTL、心跳及工具超时必须为正数，心跳超时须大于心跳间隔。静态检查不证明端口可绑定、relay 已启动或远端可连接。

## 19. meta：程序维护的标记

`/meta` 是任意 JSON 对象。执行代码已知使用 `meta.example`（示例模板标记）；旧 `meta.workspace_bootstrapped` 标记保留但不再触发自动回填。所有现有配置都仍标记 example 时，普通启动会要求用户完成配置。不要为“让检查变绿”自动删除用户尚未完成配置的标记。

## 不属于 config.yaml 的项目

- `Config.notes_workspace_max`、`notes_global_max`、`notes_inject` 在内部有默认值和消费者，但当前 YAML loader/schema 没有入口；不能发明 `notes.*` 配置。
- 没有通用 `hooks`、`plugins`、`subagents`、`retry`、`timeout`、`skills.paths`、`env` 顶层分区；未知字段会被启动与检查共同拒绝。
- 普通附件上传限额、VS Code 扩展自身设置、终端快捷键/主题不因为内部存在常量就自动成为 YAML 配置。
- MCP HTTP/SSE 传输、任意模型参数 `extra_body` / `headers` 不在当前配置结构中。

## 配置 skill 的操作路径

1. 用 `describe` 查询运行中版本的字段定义，用 `inspect` 确认全局、工作区、专用启动文件及合并结果。
2. 先明确修改范围：项目专属设置写工作区；所有项目的默认值写核心所在主机的用户配置；显式文件有最高优先级。删除覆盖项可恢复继承。
3. 使用普通文件读取、编辑工具修改目标 YAML，保留注释和格式，避免覆盖别人的新修改。编辑器中的未保存内容可用 `check.documents` 检查，不产生候选文件。
4. 运行 `rcoder config check`；格式/引用有效与模型在线可用分别判断。只有需要确认模型连接时才显式执行 `--check model --profile <name>`。
5. 保存后报告具体范围和“下次核心启动生效”，根据任务授权选择合适时机重启。不存在通用热重载；不要把 skills reload 当成配置重载。

配置 API 只保留 describe/inspect/check，无专用 LLM 写配置工具、字段权限分支、候选状态机或验证有效期。普通文件/shell 权限及其审批仍然适用。模型可以按授权编辑配置，接口不能被当作阻止文件修改的安全边界。不要把凭证输出到对话，也不要将脱敏占位值写回原文件。

## 接口能力与验证边界

- `describe.api_version` 为 2，能力包括 `buffer_checks` 与 `profile_probes`；旧写配置协议已移除。
- 启动、inspect、check 共用严格解析与合并校验；读取不会生成示例文件或补写工作区字段。
- static 覆盖类型、范围、引用、LSP 语言名称、relay 地址/超时与推理重放模式，不验证外部服务可用性。
- startup 在隔离进程中构造所有 profile 的 provider 客户端，不启动 Agent、恢复 Goal、运行 hooks 或启动 MCP/LSP。
- model 使用对应 profile 的实际请求参数，每个目标最多 20 秒，每次可选择 1–8 个 profile，会消耗提供方 Token。失败或 unknown 不等于 YAML 无效，也不会禁止保存。
- 连通性测试不覆盖工具调用、图片、MCP/LSP、hooks 或所有实际任务。检查结果不缓存为后续修改的权限。
- VS Code 默认展示当前工作区，把全局默认设置折叠展示，并提示专用启动文件的优先级。原生编辑器负责修改与保存；缓冲区变化会清除旧检查结果。
- 重启前检查已保存文件，未保存/无效配置或执行中的任务会阻止重启，保留当前核心。无自动热重载。可通过原生撤销或已有文件时间线恢复；时间线不保证覆盖外部编辑。
- 以前开发版本产生的私有备份原样保留，但新接口不再创建、应用或管理候选与备份。

## 源码依据

- [字段类型和配置校验](../reuleauxcoder/domain/config/models.py)、[默认值与内置模式/审批规则](../reuleauxcoder/domain/config/schema.py)
- [管理 schema 与脱敏](../reuleauxcoder/services/config/definition.py)、[YAML 加载与合并](../reuleauxcoder/services/config/loader.py)、[兼容迁移](../reuleauxcoder/compat/config_migration.py)
- [纯验证器](../reuleauxcoder/services/config/validation.py)、[隔离探测](../reuleauxcoder/services/config/probe.py)、[模型请求构建与探测](../reuleauxcoder/services/llm/factory.py)
- [只读配置检查服务](../reuleauxcoder/app/configuration.py)、[审批规则匹配](../reuleauxcoder/domain/approval_engine.py)
- [图片配置](../reuleauxcoder/domain/images.py)、[LSP 配置](../reuleauxcoder/extensions/lsp/config.py)、[技能发现](../reuleauxcoder/extensions/skills/discovery.py)
