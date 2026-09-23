# ReuleauxCoder TypeScript client

`@reuleauxcoder/client` 是仓库内部共享的 JSON-RPC 客户端，供 TUI 和后续宿主使用。
没有运行时 npm 依赖，不包含 React、Ink、界面状态、进程启动或 Python 业务实现。
目前不独立发布 npm 包，也不包含 VS Code 扩展。

## 入口

| 导入路径 | 内容 | 环境 |
| --- | --- | --- |
| `@reuleauxcoder/client` | `RuntimeClient`、`MessagePeer`、`RpcError`、wire 编解码及公开类型 | 浏览器或 Node |
| `@reuleauxcoder/client/node` | `RpcPeer` stdio framing、`attachImageFile` / `attachFile` 本地文件适配 | Node |

Node 入口通过 package exports 的 `node` 条件暴露，浏览器打包不能误引入。
浏览器入口的声明文件可以在没有 `@types/node` 的项目中使用。
数据类型保留现有 Python wire tags；本次抽取没有改变 RPC 协议。

## 消费与构建

同仓库消费者通过本地依赖引用：

```json
{
  "dependencies": {
    "@reuleauxcoder/client": "file:../reuleauxcoder-client"
  }
}
```

构建工具由消费者提供，避免为零依赖内部包再安装一套工具链。消费者安装
TypeScript 和 `@types/node` 后，在自己的包目录执行：

```sh
tsc -p ../reuleauxcoder-client/tsconfig.json --typeRoots ./node_modules/@types
```

该命令生成 client 的 `dist/` JavaScript 和类型声明。TUI 的 `npm ci`、
`dev`、`build`、`bundle`、`check` 与 benchmark 脚本已接入此步骤，原有开发命令不变。
后续消费者应在自己的构建之前执行同一步骤。Node 适配器随 TUI 在 Node 22/24 CI 中验证。

发布前端时，将该包打入宿主 bundle，或同时携带 client 的 `package.json` 和 `dist/`。
Python wheel/sdist 继续携带独立 TUI bundle，终端用户无需安装此 npm 包。

## 使用

宿主创建并持有后端子进程，再连接它的 stdio：

```ts
import {RuntimeClient, type UIProfile} from '@reuleauxcoder/client';
import {RpcPeer} from '@reuleauxcoder/client/node';

const profile: UIProfile = {
  ui_id: 'vscode',
  display_name: 'ReuleauxCoder VS Code',
  capabilities: ['text_input', 'stream_output'],
};
const client = new RuntimeClient(new RpcPeer(child.stdout, child.stdin));
await client.initialize(profile);
```

`child` 由宿主创建；宿主也负责 cwd、配置路径、启动错误、stderr 和进程退出。
每个前端必须显式传入实际支持的 UI profile；TUI 的 profile 位于
`reuleauxcoder-tui/src/profile.ts`。渲染性能采样名称同样由调用方传入。

在浏览器消息宿主中，可用 `new MessagePeer(sendMessage)` 创建客户端，并将收到的
完整 JSON-RPC envelope 传给 `peer.receive(message)`。发送与接收的是 wire JSON；
`decode()` 后的对象包含内部 Symbol 类型标记，不应用跨进程序列化代替 wire 编解码。

图片使用 `client.uploadImage({name, size, read})` 传入有界字节源；浏览器可读取
`File/Blob`，Node 的 `attachImageFile` 负责读取前端本地文件，路径不会传给后端。

普通附件使用相同字节源调用 `client.uploadAttachment({name, size, read})`，Node 可用
`attachFile(client, path)`。默认上限 64 MiB、单块最多 256 KiB，保存到后端工作区
`.rcoder/attachments/<session-id>/<attachment-id>/<name>`，返回相对路径及元数据。
上传不会自动提交聊天或解析内容；宿主按需把路径放进草稿。多个附件应依次上传。
完整合约、Remote 路径语义和资源限制见 [普通附件上传](../docs/attachments.md)。

## 生命周期

- 一个客户端只初始化一次。宿主持有连接，视图挂载时订阅事件，卸载时用 `off` 解绑。
- `client.interactions` 保留待处理的反向交互；宿主通过 `client.answer` 回复。
- `client.close()` 关闭传输；`client.shutdown()` 先请求后端保存退出，再关闭传输。
- 视图卸载不调用这两个方法。关闭 stdio 会让现有后端走 EOF 清理流程。
- 当前不提供后台 daemon、多客户端会话或自动重连。

## 验证

目前沿用 TUI 的测试工具链运行共享客户端回归：

```sh
npm ci --prefix reuleauxcoder-tui
npm test --prefix reuleauxcoder-tui
```

测试覆盖公开入口的独立 Node 消费、无 Node 类型的浏览器消费、浏览器打包边界、
双向 RPC、审批、图片与普通附件字节上传、分片与帧大小、退出保存，以及真实 Python/TUI 集成。
