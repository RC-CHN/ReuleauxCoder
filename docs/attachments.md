# 普通附件上传

普通文件通过 JSON-RPC 上传到**后端工作区**，用于后续 VS Code/Tauri 等宿主。
VS Code Remote 场景下，该目录位于运行 rcoder 的开发机上。已经在开发机上的文件
可以直接使用路径，无需再上传。图片需要模型视觉理解时，继续使用 `uploadImage`。

## TypeScript 客户端

浏览器传入有界字节源，不需要 Node API：

```ts
const attachment = await client.uploadAttachment({
  name: file.name,
  size: file.size,
  read: async (offset, length) =>
    new Uint8Array(await file.slice(offset, offset + length).arrayBuffer()),
});
// attachment.path: .rcoder/attachments/<session-id>/<attachment-id>/<name>
```

Node 宿主可以使用 `@reuleauxcoder/client/node` 的 `attachFile(client, path)`。
此处 `path` 属于调用该适配器的宿主：Remote Extension Host 的本地路径在开发机上；
laptop 上粘贴的附件应由 Webview 分块提供字节，不能只传 laptop 的绝对路径。

返回 `AttachmentReference`：`attachment_id`、`name`、`mime_type`、`size_bytes`、`path`。
`path` 使用 `/` 分隔，相对于后端工作区；会话 ID 使用现有持久化规则映射为安全目录名。
每次上传生成独立 ID，同名文件不会覆盖。MIME 由文件名推断，只是提示，不是内容验证。

上传本身不提交聊天、不将文件内容加入模型上下文。宿主可将返回路径插入草稿，随文字
提交，让模型按需使用现有文件工具。该入口不解析文档、不解压压缩包、不执行附件。

## RPC 合约

`initialize` 返回 `attachment_uploads: true` 时可用，旧后端会由客户端明确报错。

| 方法 | 参数 | 返回 |
| --- | --- | --- |
| `attachments.begin` | `session_id`, `session_generation`, `name`, `size_bytes` | `upload_id`, `chunk_bytes` |
| `attachments.append` | `upload_id`, `offset`, `data`（当前块的 Base64） | 下一个字节偏移 |
| `attachments.complete` | `upload_id` | tagged `AttachmentReference`，使用共享 codec 解码 |
| `attachments.cancel` | `upload_id` | `null`，可重复调用 |

当前每个连接保留一个未完成的普通附件上传；新的有效 `begin` 会关闭上一个临时文件。
宿主应依次上传多个附件，并等待每块确认后再发送下一块。图片有独立上传槽位。
客户端在成功或失败后均调用 `cancel`；已完成的附件不会被取消删除。

## 大小、内存与清理

- 默认单文件上限 **64 MiB（67,108,864 字节）**，当前是固定协议限制，允许空文件。
  客户端先检查大小，后端独立验证声明值，拒绝负数、非整数和超限值。
- 原始分块最多 **256 KiB**。后端先限制 Base64 文本长度，再严格解码，校验解码大小、
  精确偏移和累计大小；实际字节数不能超过声明值。收到全部声明字节后才能完成。
- 客户端按需读取一块并等待确认，后端逐块写磁盘临时文件；完成时也以 256 KiB
  缓冲复制并在目标目录内重命名发布，没有整个附件的 Base64/字节聚合或内容解析。
  stdio 仍使用现有有界 JSON 帧（16 MiB 上限），不是把整个附件放进一条消息。
- 上传绑定会话 generation 和工作区；后续请求发现切换时拒绝并关闭临时文件。
  取消、替换上传和后端正常退出/连接 EOF 清理同样关闭未完成文件。
- 文件名必须是单个可移植组件，拒绝路径分隔符、保留名和超过 255 UTF-8 字节的名字；
  拒绝附件目录的符号链接和越出工作区的路径。客户端不能选择保存目录。
- 已完成附件保留在工作区，不随会话切换自动删除；当前没有总磁盘配额或附件 GC。
  后续增加预览/解析器时，须另行限制解析后的大小、资源使用与运行时间。
