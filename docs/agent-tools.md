# Agent 工具使用说明

这些工具使用现有 workspace、LSP、replay 和子 Agent 控制面，不建立额外记忆或任务状态。所有读取都有边界；文件修改仍遵循审批和工作区限制。

## 查看当前模型上下文

`context_inspect` 不需要参数：

```json
{}
```

结果包含当前压缩视图的消息和角色数量、token 估计、请求输入上限、history/cache 版本、最新 checkpoint 及保留轮次。`images.withheld_from_requests` 表示历史中有图片，但当前文字模型不会把它们发送给 Provider。

该工具只描述模型当前视图。人类 transcript 和原始事件不会因压缩被改写；需要原文时使用 `history_read`、`history_search` 和 `artifact_read`。

## 按路径读取图片

`view_image` 只接受 workspace 文件路径：

```json
{"path": "assets/screen.png"}
```

路径按当前 workspace host 解析，可以是绝对路径或 workspace-relative 路径。本地与远端都通过有界 `WorkspacePort.read_bytes` 读取普通文件，再导入现有 ImageStore；远端 peer 需要支持 `workspace.fs.read_bytes`，旧 peer 会明确提示升级。上传的剪贴板图片同样位于 `.rcoder/attachments` 并使用 path 引用，因此粘贴、历史和主动读图共用 variant、缓存和 retention。

读取失败会区分文件不存在、目录、非图片、超限和原始缓存淘汰。需要看清小字时传入原图坐标区域：

```json
{"path": "assets/screen.png", "region": [100, 80, 640, 360]}
```

`full_resolution=true` 保留像素尺寸，但超出精读预算时会失败。初始文字模型或切换到文字模型时，canonical history 保留 path，Provider 只收到明确的文字占位，不会收到图片字节；切回视觉模型后按 `history` 或 `user_turn` 策略恢复。不得根据占位声称已经看过图片。新调用不暴露 attachment ID；旧会话仅保留兼容读取。

## 检查子 Agent

`read_agent` 读取一个当前根 Agent 可见的子任务：

```json
{"job_id": "sj_123", "offset": 0, "limit": 40}
```

`job_id` 来自 `spawn_agent` 或 `list_agents`。结果先给出运行状态、投递阶段、最近进度和预算；存在 checkpoint 时继续返回有界 transcript。`limit` 最大为 200，使用返回的 `next_cursor` 继续翻页。

运行中的子任务尚无 checkpoint 时返回 `availability=no_checkpoint`，这表示暂时无法读取 transcript，不表示任务失败。该工具不等待完成、不发送消息，也不重复消费已经自动投递的最终结果。

## LSP 查询

所有 LSP 操作都需要 `filePath`，用于选择语言服务器和 workspace。位置使用编辑器习惯的 1-based `line` 和 `character`。原有 `goToDefinition`、`findReferences`、`documentSymbol` 保持可用。

### 工作区符号搜索

```json
{
  "operation": "workspaceSymbol",
  "filePath": "reuleauxcoder/domain/agent/agent.py",
  "query": "AgentLoop"
}
```

`workspaceSymbol` 不需要位置。结果最多显示 50 个符号，并报告省略数量。

### 调用层级

```json
{
  "operation": "callHierarchy",
  "filePath": "reuleauxcoder/domain/agent/agent.py",
  "line": 100,
  "character": 10,
  "direction": "both"
}
```

`direction` 可选 `incoming`、`outgoing`、`both`。工具先执行 `textDocument/prepareCallHierarchy`，再请求相应的 calls；最多处理五个入口符号。

### 批量引用及源码上下文

```json
{
  "operation": "batchReferences",
  "filePath": "reuleauxcoder/domain/agent/agent.py",
  "positions": [
    {"filePath": "reuleauxcoder/domain/agent/agent.py", "line": 100, "character": 10},
    {"filePath": "reuleauxcoder/domain/agent/loop.py", "line": 50, "character": 8}
  ],
  "context_lines": 1,
  "include_declaration": true
}
```

`positions` 最多 20 项；省略时使用顶层 `filePath`、`line`、`character`。`context_lines` 为 0–3，默认 1，源码通过现有 workspace port 读取。引用总数最多展开 50 项，其余明确报告省略。服务器不可用、超时和取消仍是不同结果。

## 多文件补丁

`apply_patch` 支持 `Update File` 和 `Add File`，一个 update 可包含多个 `@@` hunk：

```text
*** Begin Patch
*** Update File: src/a.py
@@
 def run():
-    return old()
+    return new()
@@
-OLD_FLAG = True
+OLD_FLAG = False
*** Add File: src/b.py
+def helper():
+    return 1
*** End Patch
```

调用参数：

```json
{
  "patch": "<上述 patch 文本>",
  "expected_revisions": {
    "src/a.py": "<64 位 SHA-256>"
  }
}
```

使用规则：

- 先读取目标文件。update hunk 使用精确且唯一的上下文，按 patch 顺序应用；找不到或不唯一时不会写入任何文件。
- `Add File` 只接受 `+` 行，目标已存在时失败。删除文件继续使用已有工作区能力，本工具不隐式删除。
- `expected_revisions` 可省略。提供时 key 必须是 patch 中的原始路径，value 是完整 SHA-256；未知路径、格式错误或哈希不匹配都在写入前失败。
- 即使未提供显式哈希，工具也会保存当前 snapshot，并通过 `write_text_verified` 在每次写入时再次检查 revision。
- 最多 20 个文件、每个文件 200 个 hunk、总 patch 256,000 字符。所有文件先解析和预检，但写入不是全局事务。若后续文件发生外部竞争，结果列出 `applied_files`、`failed_file` 和 `unattempted_files`；已写文件不会伪装成已回滚。
- 默认审批策略为 `require_approval`。子 Agent 调用时沿用父级策略并提供 `reason`。成功后每个文件都由现有 LSP edit observer 获取有界诊断；诊断超时不会改变已成功的写入结果。

多文件结果在 `file_results` 中给出每个文件的 receipt 和 diff，并使用 `resolved_paths` 驱动诊断。单文件结果保留现有 `diff` 和 `mutation_receipt` 形状。
