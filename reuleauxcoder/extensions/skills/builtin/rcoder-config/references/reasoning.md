# 推理、thinking、返回与历史回放

本文描述当前 rcoder 的实现，不保证任意供应商或网关支持相同参数。先确定 profile 的 `provider` / `request_mode` 和继承后的最终字段；只改目标 profile，避免通过 `app` 意外改变子模型或审批模型。

## 五层机制

| 用户目标 | 相关设置 | 实际作用 |
| --- | --- | --- |
| 让模型多花或少花计算 | `reasoning_effort`、`reasoning_effort_values`、`reasoning_effort_param` | 请求侧参数；支持哪些值取决于协议、SDK 和目标模型 |
| 开关兼容接口的 thinking | `thinking_enabled` | 没有非空 effort 时发送 `thinking.type`；三态 `null` / `true` / `false` |
| 接收、保存可见推理 | `preserve_reasoning_content` | 控制推理文本收集、流式回调及 `LLMResponse.reasoning_content` |
| 人类界面怎么显示 | `ui.reasoning_display` | `hidden` 隐藏、`indicator` 状态提示、`inline` 展示；具体前端还有自己的折叠规则 |
| 下一轮给提供方带什么 | `reasoning_replay_mode`、`backfill_reasoning_content_for_tool_calls`、`reasoning_replay_placeholder` | 修整历史中的可见 `reasoning_content`；Responses 原生加密回放项是另一条路径 |

“思维链返回”应具体到提供方实际返回的字段：可能是可见推理、摘要，也可能没有文本。rcoder 不会还原隐藏推理，也不会把占位符当成真实思考。把显示改为 inline 无法让上游开始返回未提供的内容。

## 请求参数的优先级

对于每次普通请求及配置模型探测，rcoder 使用同一个构建器：

1. `reasoning_effort` 为非空字符串：先查 `reasoning_effort_values` 映射，找不到则传原字符串；此分支不发送 `thinking_enabled`。例如 `thinking_enabled: false` 与 `reasoning_effort: high` 同时存在，仍发送 effort。
2. effort 为 `null` 或空字符串时，若 `thinking_enabled` 非 `null`，发送 `extra_body: {thinking: {type: enabled|disabled}}`。
3. 两者未指定时不发送这两个控制参数，使用提供方默认行为。不等于禁用推理。

推荐用 `reasoning_effort: null` 明确清除从 `app` 继承的 effort，再控制 thinking；不要靠空字符串表达语义。两者同时设置不仅会遮蔽请求开关，`thinking_enabled` 还参与本地历史整理，因此通常只选适用于该协议的一种请求控制。

`reasoning_effort_values: null` 或空映射使用 `{low: low, medium: medium, high: high}`。自定义映射可以含 JSON 值，比如标签映射成数字，但本地接受不代表线上支持；默认三档映射也不是可用值白名单。`none`、`minimal`、`xhigh` 等任意非空字符串会原样传递，只有目标模型支持时才能使用。

`reasoning_effort_param` 默认 `reasoning_effort`，仅属于 profile，没有 `app.reasoning_effort_param` 或 `app.reasoning_effort_values`。它不能覆盖 `model`、`messages`、`temperature` 等保留参数；改名仍受实际 SDK 方法签名限制，不是通用 `extra_body` 注入接口。

## 三种请求协议

| provider / request_mode | effort | thinking | 返回及历史 |
| --- | --- | --- | --- |
| `openai-compatible` / `chat-completions` | 通过 `reasoning_effort_param` 对应的顶层参数传入 OpenAI SDK | 没有 effort 时通过 `extra_body.thinking.type` 发送；网关必须支持 | 收集流中的 `delta.reasoning_content`；按下面的规则整理后回传 |
| `openai-compatible` / `responses` | 固定转成 `reasoning: {effort: 值}`，忽略自定义参数名 | 不支持；没有 effort 遮蔽时，`true` 或 `false` 都会在构建请求时被拒绝，应设 `null` | 收集 reasoning summary/text 的 delta；原生 reasoning/function-call 输出项另存于 `provider_data` 并回放 |
| `anthropic` / `messages` | 当前适配器不转换 effort；它仍会先遮蔽 thinking 分支，不能靠它配置 Anthropic 推理强度 | 仅转发 `{type: enabled|disabled}`，存在 thinking 时不附带 temperature | 可收集 thinking delta，但未实现原生 thinking/signature 块的完整回放，不能承诺完整 extended-thinking 工具轮支持 |

Anthropic 当前 YAML 没有 `budget_tokens`、adaptive thinking 或 `output_config.effort` 入口。需要这些能力时说明实现边界，不虚构字段或用 `reasoning_effort_values` 冒充支持。普通 Messages 不启用 thinking 的路径仍可使用。具体模型与网关的可用参数应核对其资料，并按需做连通/工具轮验证。

Responses 始终发送 `store=false`、完整本地历史和 `include: [reasoning.encrypted_content]`，不使用 `previous_response_id` 链。`responses.state` 目前只有 `local`；`responses.cache.mode` 为 `implicit` 或 `explicit`，后者要求目标端支持显式断点。这不是 reasoning summary 级别配置，当前没有 YAML 字段可设置 `reasoning.summary`。

`temperature` 和输出预算仍是独立参数。某个 reasoning 模型不接受温度、某个网关不支持缓存扩展或加密项时，结构检查仍可能通过；模型探测/实际请求才可能发现。不要承诺只改一个 effort 就适配所有模型。

## 返回与保存

- `preserve_reasoning_content: true`（默认）：流里实际有 `reasoning_content` 才会累积，并向前端发送推理增量，最后进入响应/会话历史。不会主动要求供应商披露更多内容。
- `false`：不累积新的可见推理文本，也不调用推理 token 回调。不是仅仅“别保存到磁盘”；如果用户只是想界面清爽，先改 `ui.reasoning_display`。
- 已有历史不会因改文件被追溯清除。尤其 `thinking_enabled: true` 时，本地整理器会保留历史已有推理，即使 preserve 为 false；不要把它说成隐私清除功能。
- Responses 的 `provider_data` 独立于可见文本保留开关，关闭 preserve 不会关闭加密项收集/回放，也不会自动删除其会话存储。UI 不将加密内容显示为可读推理。
- `app.llm_debug_trace` 用于诊断是否收到推理流，不用于获得更多推理。调试内容可能包含请求、对话和工具信息，不向对话粘贴原始敏感日志；排障后按用户意图恢复设置。

## 历史整理与补位的精确含义

以下针对本地 `reasoning_content`；最终协议适配器仍决定哪些字段真正发送。

| 设置/消息 | 行为 |
| --- | --- |
| `reasoning_replay_mode: null` 或 `none` | 不强制补位；并非“一律不回放”。默认仍可能保留工具调用及同一用户轮后续 assistant 的已有推理 |
| `reasoning_replay_mode: tool_calls` 且 preserve 为 true | 工具调用 assistant 缺少字段时补位；该用户轮工具调用之后的 assistant 缺少字段也补位 |
| `backfill_reasoning_content_for_tool_calls: true` 且 preserve 为 true | 在 null/none 模式也启用同样的补位；可以理解为独立兼容开关 |
| preserve 为 false、thinking 不为 true | 移除已有可见推理字段，不补位 |
| thinking 为 true | 不执行普通的非工具 assistant 推理剥离；不单独触发缺失字段补位 |
| thinking 不为 true，普通 assistant，且本用户轮尚无工具调用 | 移除其 `reasoning_content`；这个路径不是全历史思考回放 |

补位只针对**字段不存在**，不修复已经存在的 `null` 或空字符串。默认占位 `[PLACE_HOLDER]`；`reasoning_replay_placeholder` 为 `null` 或空字符串也回退此默认值。占位既不是历史推理，也不能修复供应商要求的签名/加密状态。只有已确认端点要求字段存在、且接受占位的兼容问题才启用，不应作为通用推荐配置。

用户新消息会重置“本轮已发生工具调用”状态。历史还会进行工具调用 ID 修复、工具结果邻接校验以及空 assistant 正文补位；这与 thinking 强度无关。

## 按症状选择改动

- “不要显示思考”：保持模型参数和 preserve，改 `ui.reasoning_display: hidden`；前端自己展开的内容还受其界面状态控制。
- “省时/加大推理强度”：核对协议及模型支持后改目标 profile 的 effort；不能从 UI 显示推断模型计算量。
- “打开 thinking 没效果”：先查继承的非空 effort 是否遮蔽，再查协议、实际返回字段、preserve 和 UI。Responses 应用 effort，不能配置 thinking 布尔开关。
- “工具之后缺 reasoning_content 报错”：核对提供方要求、流是否返回、preserve、原历史字段及 replay；不要直接全局开补位掩盖协议不兼容。
- “换成 Anthropic 要完整 extended thinking”：当前适配器能力有限，明确缺少预算/签名回放支持，不通过伪字段让用户误以为完成。
- “只想不保存推理”：说明可见文本、旧历史和 Responses 加密状态三者边界；当前没有覆盖三者的一键配置。
