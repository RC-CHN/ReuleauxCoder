# ReuleauxCoder for VS Code

右侧会话，中央原生 diff。支持本机以及 Remote SSH、WSL 和 Dev Containers；扩展和核心运行在工作区主机，界面运行在你正在使用的 VS Code 中。需要 VS Code 1.106 或更新版本。

## 开始使用

1. 在工作区窗口中安装 VSIX，打开右侧 **Reuleaux → 会话**。Remote 窗口需安装到对应的远端环境。
2. 扩展自动寻找并启动核心：`reuleaux.corePath` → 扩展托管安装 → 主机 PATH 中的 `rcoder`。已有核心必须支持本版本扩展的 RPC 能力。
3. 如果缺少核心或版本不兼容，点击 **安装兼容核心**。扩展校验附带 wheel，并在工作区主机的扩展存储中建立私有 Python 环境；有 uv 时使用 uv，否则使用 Python venv/pip。需要 Python 3.10+，首次安装依赖需要网络。没有 uv 的 Debian/Ubuntu 主机还需对应的 `python3-venv` 包。安装失败不会替换已有托管核心。
4. 使用工作区主机的 `~/.rcoder/config.yaml` 配置模型。核心首次启动缺少配置时会生成示例；也支持工作区 `.rcoder/config.yaml`，或通过 `reuleaux.coreArguments` 传入 `--config`。配置错误通过启动信息与 **查看日志** 展示，修正后重试。

也可以通过 **选择已有安装** 指定主机上的 `rcoder`，或配置 `reuleaux.corePath` 为 Python 可执行文件、`reuleaux.coreArguments` 为 `["-m", "reuleauxcoder"]`。参数逐项传递，不经过 shell。

## 日常操作

- Enter 立即清空输入并显示发送消息；Shift+Enter 换行。任务执行期间的消息作为 steering 补充要求。新草稿不受旧消息确认影响，失败消息可按原 ID 重试。
- 粘贴图片、拖放本机文件，或点输入框旁的 `＋`。文件字节分块上传到工作区主机，不会把本机路径误当作远端路径。上传中也能发送，消息会等待自己的附件完成；失败时可以重试或退回草稿。单文件上限 64 MiB，图片支持 PNG/JPEG/WebP，并要求模型支持图像。
- 文件管理器与编辑器右键可添加文件或选区；诊断灯泡提供“用 Reuleaux 解释或修复”。未保存内容作为标记过的编辑器快照加入上下文。
- 修改提案自动打开中央原生 diff，保留当前输入焦点。用编辑器右上角或审批卡片批准/拒绝。两侧是冻结的只读版本；批准后核心再次核对文件版本并实际写入。多文件提案可从 **等待审批** 树选择。
- 如果目标文件有未保存编辑，不能直接批准覆盖；保存后拒绝旧提案，并让核心基于新内容重新提案。自动批准及子代理的内置写入工具同样受保护；任意 shell 命令的文件写入不在这项保护范围内。
- 模型按钮直接打开模型选择；顶部 `⋯` 使用核心命令目录。历史会话、新会话、停止任务、查看日志均有原生命令。会话可在编辑区打开；关闭视图不会停止核心。
- 界面默认跟随 VS Code 显示语言：中文或英文，其余语言回退英文。扩展按钮、状态、安装引导与原生命令有翻译；模型回复、源代码、核心命令目录和原始诊断保留原文。

多根工作区首次使用时选择一个根目录作为会话归属。目录以外的文件请使用上传入口。暂不支持虚拟工作区、后台 daemon 或窗口关闭后继续执行；正常退出等待核心保存，重新打开后可从历史恢复。原生 diff 限制为每侧 4 Mi 字符，超限使用文本预览。

## 设置

| 设置 | 用途 |
| --- | --- |
| `reuleaux.corePath` | 工作区主机上的核心/Python 可执行文件 |
| `reuleaux.coreArguments` | 额外 argv；扩展自动附加 `--rpc-stdio` |
| `reuleaux.pythonPath` | 安装托管核心时使用的 Python |
| `reuleaux.autoStart` | 打开会话时自动启动，默认开启 |
| `reuleaux.openDiffAutomatically` | 自动展示原生 diff，默认开启 |

## 开发和验证

从仓库根目录运行 `uv sync --frozen --extra dev`，然后：

```sh
cd reuleauxcoder-vscode
npm ci
npm test
npm run test:extension
npx playwright install chromium
npm run test:webview
npm run package
npm run test:install
```

`package` 会构建扩展、从当前源码打包兼容核心 wheel，再生成 VSIX。无运行时 npm 安装步骤。`test:install` 在临时私有环境安装打包核心并检查真实 RPC 初始化和退出，需要下载 Python 依赖。其余测试使用确定性模型夹具和真实 Python RuntimeServer，不访问模型服务。Linux 扩展宿主测试需要显示器或 `xvfb-run -a npm run test:extension`。可设置 `VSCODE_EXECUTABLE_PATH` 使用本地 VS Code，默认下载最低支持版本；`RCODER_TEST_PYTHON` 指定夹具 Python，`PLAYWRIGHT_CHROMIUM_EXECUTABLE` 可指定浏览器。

CI 在 Linux 和 Windows 上运行核心协议、真实 VS Code 宿主、Chromium 中英界面与打包安装测试，Windows 同时覆盖 Node 22/24。工作区和私有安装目录包含中文及空格；原生 diff 检查 LF/CRLF 从预览到批准落盘的保留，脏文件保护检查 Windows 路径大小写差异。TUI 也在两个系统的 Node 22/24 上运行，POSIX PTY 用例仅在 Linux 执行。

## English

ReuleauxCoder runs a persistent Python core on the workspace host, locally or inside VS Code Remote SSH/WSL/Containers. Chat defaults to the secondary sidebar; change approvals open frozen before/after documents in the native editor. Requires VS Code 1.106+ and host Python 3.10+.

Open **Reuleaux → Conversation** to connect automatically. If the core is missing or incompatible, choose **Install compatible core** to install the checksum-verified bundled wheel in private extension storage using uv or venv/pip. Configure your model in the host's `~/.rcoder/config.yaml` (a first-run example is generated), then retry. The settings table above supports explicit executable, arguments and Python selection. No shell command interpolation is used.

Enter shows ordinary/steering input immediately and preserves the next draft. Files and clipboard images transfer as bounded byte chunks to the workspace host; sending during an upload waits for that message's attachments in the background. Failed messages can retry with the same ID or return to the draft. Context actions capture selected code and diagnostics. Approval revalidates disk content; built-in edits refuse to overwrite known unsaved editor documents. Closing a view leaves the host session connected. UI language follows VS Code (Chinese/English, English fallback); provider output and core catalogs remain verbatim.

Build and validation commands are shared above. Linux and Windows CI run real core RPC, native VS Code diff and buffer handling, cancellation, bilingual Chromium input/upload tests and packaged installation; Windows covers Node 22/24. Tests use Unicode/space-containing paths, preserve both LF and CRLF through approval, and check Windows path casing in dirty-file protection. TUI tests also run on both systems with Node 22/24; POSIX PTY cases run on Linux only. Genuine Remote transports still require validation in their respective environments; local extension-host tests do not simulate an SSH server.
