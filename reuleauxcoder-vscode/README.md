# ReuleauxCoder for VS Code

右侧会话，中央原生 diff。支持本机以及 Remote SSH、WSL 和 Dev Containers；扩展和核心运行在工作区主机，界面运行在你正在使用的 VS Code 中。需要 VS Code 1.106 或更新版本。

## 开始使用

1. 在工作区窗口中安装 VSIX，打开右侧 **Reuleaux → 会话**。Remote 窗口需安装到对应的远端环境。
2. 扩展自动寻找并启动核心：`reuleaux.corePath` → 扩展托管安装 → 主机 PATH 中的 `rcoder`。启动握手检查核心发行版本、编辑器接口修订号和 RPC 能力，通过后才允许恢复目标与发送消息。发行版本至少与扩展一致；接口修订号也能识别版本号相同但缺少必要修复的旧构建。
3. 如果缺少核心或版本不兼容，在会话内点击 **安装兼容核心／更新核心**。使用 VSIX 附带的配套 wheel（校验 SHA256），不直接拉取 GitHub latest；在工作区主机的扩展存储中建立新的私有 Python 环境，有 uv 时使用 uv，否则使用 Python venv/pip。安装完成后切换到托管核心，已有外部安装保留，安装失败不会替换已有托管核心。需要 Python 3.10+，首次安装依赖需要网络。没有 uv 的 Debian/Ubuntu 主机还需对应的 `python3-venv` 包。
4. 使用工作区主机的 `~/.rcoder/config.yaml` 配置模型。核心首次启动缺少配置时会生成示例；也支持工作区 `.rcoder/config.yaml`，或通过 `reuleaux.coreArguments` 传入 `--config`。配置错误通过启动信息与 **查看日志** 展示，修正后重试。

也可以通过 **选择已有安装** 指定主机上的 `rcoder`，或配置 `reuleaux.corePath` 为 Python 可执行文件、`reuleaux.coreArguments` 为 `["-m", "reuleauxcoder"]`。参数逐项传递，不经过 shell。

## 日常操作

- Enter 立即清空输入并显示发送消息；Shift+Enter 换行。任务执行期间的消息作为 steering 补充要求。新草稿不受旧消息确认影响，失败消息可按原 ID 重试。
- 粘贴图片、拖放本机文件，或点输入框旁的附件图标。文件字节分块上传到工作区主机，不会把本机路径误当作远端路径。上传中也能发送，消息会等待自己的附件完成；失败时可以重试或退回草稿。单文件上限 64 MiB，图片支持 PNG/JPEG/WebP，并要求模型支持图像。
- 文件管理器与编辑器右键可添加文件或选区；诊断灯泡提供“用 Reuleaux 解释或修复”。未保存内容作为标记过的编辑器快照加入上下文。
- 修改提案出现在会话中的审阅卡片，点文件行打开中央原生 diff；也可自动打开并保留当前输入焦点。默认仅批准本次；展开 **授权范围与反馈** 可选择核心提供的会话范围，或拒绝并说明原因。两侧是冻结的只读版本，批准后核心再次核对文件版本并实际写入。输入框附近的待处理入口可跳回卡片。
- 如果目标文件有未保存编辑，不能直接批准覆盖；保存后拒绝旧提案，并让核心基于新内容重新提案。自动批准及子代理的内置写入工具同样受保护；任意 shell 命令的文件写入不在这项保护范围内。
- 点击输入框旁的功能图标即可浏览分组菜单，也可输入 `/` 搜索；鼠标、Tab 和方向键均可操作，Esc 关闭并保留草稿。模型、模式、权限、目标、技能、MCP、进程及会话面板都在会话内展开，参数直接在表单中填写。普通确认、选项与秘密输入也在会话中完成，不再使用顶栏连续弹窗；秘密输入不写入视图存储。
- 工作概况默认收起，点击顶部按钮查看计划、进展、进程输出、子任务、Git、诊断、上下文用量和权限状态。输入框从单行开始，随内容自动增高；完成的目标自动收起，活动目标的暂停/继续按钮保留在输入框上方。会话可在编辑区打开；关闭视图不会停止核心。
- 工具权限面板按工具显示策略，顶部切换当前会话和工作区默认；可直接选择自动允许、每次询问、禁止或提醒后执行，也可恢复继承。显示规则来源和较大授权范围，修改后从核心刷新实际结果。审批放在会话中，不再提供独立的 Pending Reviews 视图。
- 回复支持 Markdown 标题、列表、表格、引用和可复制代码块；文件名、相对路径和行内代码引用可直接打开工作区文件，支持 `src/main.ts:42:3` 与 `docs/guide.md#L12-L18`；明确的 HTTP 链接在浏览器打开。禁用原始 HTML 和命令链接，外部图片不会自动加载。界面跟随 VS Code 主题和显示语言：中英文，其余语言回退英文。扩展控件和常用内置面板标签有翻译；模型回复、源代码、动态名称与原始诊断保留原文。

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

浏览器回归还验证鼠标菜单、IME、嵌套面板、表单草稿、秘密输入、审批范围和 Markdown 安全，并在 `artifacts/vscode-concept/` 生成中英离线 HTML 预览及暗色、浅色、窄屏截图。预览使用实际界面 bundle 和标明为演示的协议数据，不启动核心或修改文件。

本地化覆盖内置菜单、表单、目标控制、MCP／技能状态、进程详情、审批结果与常见操作提示；数字和耗时按语言显示。模型名、配置名、技能名、工具标识、路径、用户目标及程序／模型输出保留原文。翻译测试直接检查真实核心的命令目录与参数，并验证这些动态内容不会被误翻译；未知插件文案和原始诊断保留原文。

## English

ReuleauxCoder runs a persistent Python core on the workspace host, locally or inside VS Code Remote SSH/WSL/Containers. Chat defaults to the secondary sidebar; change approvals open frozen before/after documents in the native editor. Requires VS Code 1.106+ and host Python 3.10+.

Open **Reuleaux → Conversation** to connect automatically. If the core is missing or incompatible, choose **Install compatible core** to install the checksum-verified bundled wheel in private extension storage using uv or venv/pip. Configure your model in the host's `~/.rcoder/config.yaml` (a first-run example is generated), then retry. The settings table above supports explicit executable, arguments and Python selection. No shell command interpolation is used.

Startup checks the core release version (at least the extension version), editor integration revision and required capabilities before resuming goals or accepting messages. The revision detects older builds even when the release number matches. **Update core** installs the matching wheel included in the VSIX into a new private environment on the workspace host, then switches to it; it does not fetch GitHub latest or overwrite an external installation. Failed installation preserves the existing managed core. Python dependencies may still require network access.

Enter shows ordinary/steering input immediately and preserves the next draft. Files and clipboard images transfer as bounded byte chunks to the workspace host; sending during an upload waits for that message's attachments in the background. Failed messages can retry with the same ID or return to the draft. Context actions capture selected code and diagnostics. Approval revalidates disk content; built-in edits refuse to overwrite known unsaved editor documents. Closing a view leaves the host session connected.

Click the command icon beside the composer or type `/` to search grouped commands. Models, modes, permissions, goals, skills, MCP, processes and sessions open in conversation panels with clickable rows and parameter forms. Confirmation and secret input also stay in the conversation; secrets never enter saved webview state. Review cards link each file to its native diff, default to approval once, and expose backend-provided session scopes and denial feedback. The work overview is collapsed by default and opens from the compact header. The composer starts at one line and grows with input; completed goals collapse automatically. Active goal controls stay above the composer. Tool permissions use a searchable list with direct per-tool policy controls and session/workspace tabs; inherited rules and broad scopes remain explicit. Changes refresh core-owned facts, and the separate Pending Reviews view is removed. Markdown replies render lists, tables and copyable code without executing HTML, command links or remote images. Plain filenames, inline-code references and relative Markdown links open workspace files in the native editor, including `src/main.ts:42:3` and `docs/guide.md#L12-L18`; explicit HTTP links remain external. File navigation retains the Remote workspace authority. Theme and language follow VS Code; built-in UI supports Chinese and English, while model output and dynamic core data remain verbatim.

Build and validation commands are shared above. Linux and Windows CI run real core RPC, native VS Code diff and buffer handling, cancellation, bilingual Chromium input/upload tests and packaged installation; Windows covers Node 22/24. Tests use Unicode/space-containing paths, preserve both LF and CRLF through approval, and check Windows path casing in dirty-file protection. TUI tests also run on both systems with Node 22/24; POSIX PTY cases run on Linux only. Genuine Remote transports still require validation in their respective environments; local extension-host tests do not simulate an SSH server.

Localization covers built-in command forms, goal controls, MCP/skill status, process facts, approval feedback and common runtime notices, including locale-aware counts and durations. Custom names, identifiers, paths, objectives, model text and process output remain verbatim. Coverage tests use the real core catalog and verify these content boundaries; unknown plugin text and raw diagnostics retain their original language.
