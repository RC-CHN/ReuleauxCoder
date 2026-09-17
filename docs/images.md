# 图片输入与上下文保留

CLI 和 React TUI 支持静态 PNG、JPEG、WebP。先在模型 profile 中明确启用图片能力：

```yaml
models:
  profiles:
    vision:
      model: "your-vision-model"
      api_key: "your-api-key"
      support_modal: [text, image]
    text:
      model: "your-text-model"
      api_key: "your-api-key"
      support_modal: [text]
context:
  image_retention: history # history | user_turn
attachments:
  image:
    normal_max_bytes: 262144             # 普通版本：256 KiB 二进制
    max_edge_px: 2000
    detail_max_base64_bytes: 2097152     # 精读：2 MiB Base64，约 1.5 MiB 二进制
    originals_cache_max_bytes: 1073741824 # 每会话原图缓存：1 GiB
    import_max_bytes: 67108864           # 单文件导入上限：64 MiB
    max_pixels: 40000000
```

`support_modal` 是输入模态数组，默认 `[text]`，当前可选 `text`、`image`，不根据模型名字猜测。无 profile 时可用 `app.support_modal`。切换模型同时切换能力；`/config` 可查看有效值。

## 添加和发送

直接粘贴图片路径（或由终端把拖入的文件转换成路径）即可，支持引号、转义空格、`file://` 和本机 Windows 路径。确认文件是图片后，输入框在原位置显示 `[Image #1]`；发送后的用户消息也保留这个标记。文字与图片按标记位置交错发送。普通文本、无法读取的路径保持文字，不自动请求网络图片。

TUI 中方向键跨过完整图片标记，Backspace/Delete 删除标记及对应附件；CLI 在标记末尾按 Backspace 删除附件。只有附件也可直接回车发送。`/attach <路径>`、`/detach <序号|all>` 留作备用入口。这里识别的是粘贴的路径文本，不是读取操作系统中的截图数据。

路径属于**运行前端的机器**。本地 TUI 连接 SSH 后端时，前端读取本地文件并分块上传；在 VS Code Remote 的远端终端运行 rcoder 时，路径属于远端机器。目前不提供 Windows 剪贴板桥接、原生图片粘贴或 remote peer 路径导入。上传和提交都绑定会话及 generation，切换会话后需重新添加附件。

向文字模型提交新图片会被拒绝，草稿保留，可切换模型或移除附件。处理失败会报错，不会绕过限制发送原图。

## 压缩与流量

普通版本默认最长边 2000 像素、每张最多 256 KiB。符合限制且无方向变换的图片直接复用；否则纠正 EXIF 方向，再从原图生成发送版本。PNG 类图片先尝试无损缩放，之后按 JPEG 质量 80、60、40、20 及更小尺寸逐级压缩。透明图片保留 PNG，无法满足预算时报错。动画暂不支持。

模型可用 `view_image(attachment_id, region=[x,y,width,height])` 请求原图局部，坐标以纠正方向后的原图为准。优先裁剪看不清的区域，精读版本使用独立的 2 MiB Base64 上限。`full_resolution=true` 保留像素尺寸，无法在预算内编码时报错。工具图片同样参加上下文保留和能力过滤。

发送版本按内容寻址并复用，避免反复有损压缩。原图缓存按最早写入时间淘汰；发送版本独立保存，不随原图淘汰，历史重放不受影响。新精读区域需要原图；原图淘汰后要求重新导入，不自动重读同名路径。

API 请求仍随有效历史重复携带图片。256 KiB 二进制约变成 341 KiB Base64。附件 ID 是 rcoder 内部引用，不是服务端文件 ID；prompt cache 不等于节省上传流量。需要减少后续回合流量时使用 `user_turn`。

没有请求图片总字节数或总张数限制。HTTP 413 后，当前回合依次尝试只保留最近两张、再用文字替换本次已有图片；不足三张时跳过重复的“最近两张”步骤。无图仍超限就报错。降级后的新裁图可以发送；降级仅影响请求投影，不改写历史。瞬态错误仍使用已有的有限重试机制。

历史中的 `image_payload_observed` 事件记录每次发送尝试的图片数、Base64 字节数和降级阶段，包含 413 与网络重试。它表示交给 Provider 的图片负载，不是抓包测得的网络流量，也不包含首次附件上传。

## 历史、模型切换与压缩

- `history`：图片随有效上下文保留，每次请求发送当前仍保留的图片。
- `user_turn`：仅发送当前真实用户回合的图片；工具循环、steering、自动 goal 续跑和单独切换模型不开启新回合。下一条正常用户消息开始新回合，旧图片只显示文字标记。
- 切到文字模型：用户图片、工具图片统一变成请求内的文字标记，原始消息保留引用。
- 切回图片模型：恢复符合当前策略的图片；已过期或上下文压缩已移除的图片不会重新进入请求。
- 恢复会话：从原始消息恢复引用及回合归属，不从文字模型的占位文本重建历史。
- 压缩上下文：总结器仅接收文字、附件标识和已有的文字观察，不接收图片字节，也不把附件标识当作“已看过图片”。完整历史仍可查询，压缩掉的图片不会自动重新发送。

Chat Completions 的工具图片放到完整工具结果组之后的补充用户消息中；Responses 使用 `input_image`，Anthropic 使用原生 `image` / `tool_result` 块。三种协议从同一本地历史投影，保留工具调用关联。
