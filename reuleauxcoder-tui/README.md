# ReuleauxCoder TUI

直接粘贴 PNG/JPEG/WebP 文件路径，会在输入框插入 `[Image #1]`，发出的用户消息保留相同标记；Backspace/Delete 删除标记及附件。`/attach`、`/detach` 留作备用入口。模型配置使用 `support_modal: [text, image]`，默认 `[text]`。前端分块上传文件到后端，包括 SSH 后端；文字模型拒绝新图片时保留草稿，切换会话则清除草稿附件。详见[图片输入与上下文保留](../docs/images.md)。

独立的 React + Ink 终端前端，与 `reuleauxcoder-agent/` 同级。Python 后端拥有 Agent、命令、审批策略和会话保存；界面通过双向 JSON-RPC 收发数据。

`src/protocol/client.ts` 和 `message-peer.ts` 可用于浏览器消息桥；Node stdio 和本地图片文件读取分别在 `peer.ts`、`files.ts`。客户端可传入 UI profile，图片接口接收字节源。宿主负责后端进程与客户端生命周期，视图关闭只解绑监听。详见[前端与运行时边界](../docs/frontend-runtime-boundary.md)。

## 运行

安装发布 wheel 后直接运行 `rcoder` 或 `rcoder-tui`，需要 Node.js 22+，不需要 npm。
`rcoder` 在 Node 不可用时提示原因并回退 CLI；`rcoder-cli` 始终选择 CLI，`rcoder-tui`
则会明确报告 Node 缺失、过旧或非交互终端。Python 启动器使用工具自身的解释器启动后端。

以下是源码开发方式，需要 Node.js 22+、npm 以及本仓库已安装依赖的 Python 环境。
沿用 `rcoder` 的模型和 API 配置。在仓库根目录执行：

```sh
npm --prefix reuleauxcoder-tui ci
npm --prefix reuleauxcoder-tui run build
node reuleauxcoder-tui/dist/cli.js
```

默认启动仓库 `.venv/bin/python -m reuleauxcoder --rpc-stdio`；没有该环境时使用 `python3`。工作目录默认是启动前端时的目录。

```sh
node reuleauxcoder-tui/dist/cli.js --cwd /path/to/project
node reuleauxcoder-tui/dist/cli.js --config /path/to/config.yaml --model model-name
node reuleauxcoder-tui/dist/cli.js --resume session-id
node reuleauxcoder-tui/dist/cli.js --python /path/to/venv/bin/python
```

开发时运行 `npm --prefix reuleauxcoder-tui run dev -- --cwd /path/to/project`。

`src/ui/render.tsx` 统一管理终端帧与光标保持，启动器和渲染 benchmark 共用此入口。
Ink 7.1.1 每帧消费光标位置，适配层通过内部 `CursorContext` 在 `onRender` 绘制前
重放最近一次提交的位置，让独立动画、正文刷新及 resize 保持 IME 定位；失焦或卸载时正常清除。
输入组件仍使用公开的 `useCursor`。升级 Ink 时检查这个兼容边界；`test/cursor.test.tsx`
通过真实 ANSI 输出验证位置保持与清除。

发布构建执行 `npm run bundle --prefix reuleauxcoder-tui`，产物写入 Python 包的
`reuleauxcoder/_tui/`，再运行 `uv build` 和 `python scripts/check-distributions.py`。
bundle 包含运行时依赖及 Yoga WASM，附带第三方许可证；构建工具与 `node_modules`
不会进入发布包。源码包也包含已构建资源，从源码包安装无需运行 npm。

## 主题

默认采用琥珀工作台风格：顶部品牌与状态、左侧会话和带完整细边的输入框、宽屏右侧信息栏。琥珀色指引输入和当前操作，灰青色标记模型回复与运行活动，低对比度细线区分层级。菜单选中项使用底色，审批使用提醒色。当前版本使用纯色背景，未加入雾光。

终端至少 120 列、20 行时自动显示 30–36 列侧栏，主区至少保留 85 列。侧栏按需展示计划、子 Agent 和后台进程活动、模型与上下文用量；长计划优先展示当前步骤，更多内容通过 F2 查看。窗口变窄或变矮时侧栏收起，草稿和交互保留。侧栏直接使用会话状态，完整信息始终可在 F2 会话详情中查看。

后台进程在侧栏独立显示命令、状态、运行时间、显式超时和最近输出，最多展开三个进程，其余显示数量；空间不足时先收起输出详情。无输出时计时仍更新。窄窗口保留 `/ps` 数量入口，查看输出、输入和停止操作都通过 `/ps`。连续 poll 合并为等待状态，F4 仍可查看每次调用的完整记录；后台进程结束后只提示一次。

`shell` 默认等待五秒后返回进程句柄，未设置 `timeout` 或设置为 `0` 时没有总运行期限。打断模型轮次或取消 poll 只结束等待；进程由会话继续托管，直到自行退出、显式停止或 rcoder 正常退出。正数 `timeout` 仍会在到期后终止进程树。后台托管不提供跨 rcoder 重启的进程恢复。

侧栏先按优先级放摘要：待审批／冲突／错误 → 当前执行 → 计划当前步骤 → Git → 上下文、模型、模式和默认审批策略 → 子 Agent／进程。剩余高度再展开活动详情、Git 文件、更多计划步骤和用量；F2 保留完整数据。

Git 文件列表最多显示 4 个，剩余数量显示为 `and N more · F2`；空间不足时进一步收起，F2 保留全部已读取的文件。

Git 每 5 秒通过 JSON-RPC 读取后端工作区，复用后端有时间和输出上限的 Git 执行器，不消费模型的提交变化提示。显示分支或 detached HEAD、upstream 的本地 ahead/behind、变更数量、暂存／未暂存／未跟踪文件及冲突。文件旁两列状态分别表示暂存区和工作区（`.` 表示无变化，`?` 表示未跟踪）；同一文件可能同时有暂存和未暂存修改。增删行数为已跟踪文本文件相对 HEAD 的净变化，不包含未跟踪文件和二进制内容；无初始提交或统计失败时省略。扫描超限明确标记为不完整。不会自动 fetch；SSH 后端显示后端仓库，未提供 Git 监视器的后端不显示 Git 区。

界面采用像素 R 标识、整行执行状态色带、编号角色标签、方角代码框和贯穿全宽的输入区。至少 34 行、72 列时，启动标识展示约 2 秒，然后每 120ms 向上收起一行，回到紧凑页头并释放 3 行正文空间；每次启动只播放一次，调整窗口或切换会话不重播。较小窗口直接使用紧凑页头；底部状态条从 26 行起显示。状态来自实际连接、运行阶段、模型、模式、计划与 Git 快照；导航使用现有 F2 / F4 / slash 操作。消息编号按会话中的用户和模型消息排列，展开工具和思考不会改变编号。

连接、等待模型、思考和工具执行期间，输入框上沿有随主题配色的往返亮线，表示正在运行而非完成百分比。回到空闲时边框在约 720ms 内淡出；待审批时保持稳定提醒色，断线时使用错误色。动画定时器只更新边框组件，不改变正文布局、草稿或历史记录，空闲淡出后停止刷新。无需终端 shader。

启动连接状态变化，以及面板打开、返回、审批子页和表单步骤切换时，对应区域在约 250ms 内逐渐显亮，所有内容从第一帧就完整可读、可操作，审批快捷键保持原亮度。列表选择、过滤、滚动和后台数据刷新不重播入场动画。显亮效果使用主题已有真彩色；ANSI 命名色保持静态显示。

颜色角色统一为：`accent` 琥珀色表示操作／当前步骤，`secondary` 灰青色表示模型输出，`info` 浅蓝色表示元数据／文件路径，`success` 表示成功或新增，`error` 表示拒绝／错误／删除，`warning` 表示待处理事项，`muted` 表示辅助说明。正文保留中性颜色。`panelBackground` 用于工具与队列色带，`additionBackground` / `deletionBackground` 用于 diff；这些背景也可独立配置。

```sh
rcoder-tui --theme workbench
node reuleauxcoder-tui/dist/cli.js --theme ocean
node reuleauxcoder-tui/dist/cli.js --theme ember
node reuleauxcoder-tui/dist/cli.js --theme /path/to/theme.json
```

内置 `workbench`（默认，炭灰底色、琥珀与灰青双色）、`terminal`（使用终端自身配色）、`ocean`（冷蓝）、`ember`（暖琥珀）。`workbench` 设置界面背景和正文色，其余预设沿用终端背景和正文色。可用 `--theme terminal` 切回终端原生配色。

项目默认主题保存在前端工作目录的 `.rcoder/tui-theme.json`。可以直接写 `"ember"`，也可以继承主题并覆盖颜色：

```json
{
  "extends": "workbench",
  "accent": "#80CBC4",
  "secondary": "#93B8AC",
  "info": "#9FBAD6",
  "border": "#46524F",
  "foreground": "#DEDCD3",
  "background": "#191D1E",
  "panelBackground": "#242C2A",
  "additionBackground": "#24382F",
  "deletionBackground": "#3C2B2A",
  "muted": "#8B98AA",
  "success": "#A8C977",
  "warning": "#E8BA70",
  "error": "#F08080",
  "selectionBackground": "#253B45",
  "selectionText": "#E6F4F1"
}
```

颜色支持 `#RRGGBB`，以及 `black`、`red`、`green`、`yellow`、`blue`、`magenta`、`cyan`、`white`、`gray`、`default`。`muted: "default"` 使用终端弱化样式；背景和正文设置为 `default` 时沿用终端颜色。优先级为 `--theme` > 项目主题文件 > `workbench`。已有的自定义对象如果省略 `extends`，仍继承 `terminal`，新增色号无需补填。启动时读取配置；错误配置会给出文件和字段信息。主题定义与配色逻辑集中在 `src/ui/theme.ts`，加载逻辑在 `theme-config.ts`。

## 交互

界面采用固定输入区、可滚动会话记录、就近打开的菜单和审批面板。参考了 [Codex CLI 的命令弹窗与确认反馈](https://developers.openai.com/codex/cli/slash-commands)，组件与终端生命周期由 [Ink](https://github.com/vadimdemedes/ink) 管理。

输入 `/` 筛选一级菜单，回车进入。例如 `/model` 打开模型面板，然后选择会话模型、子 Agent 模型或默认配置；`/ps` 打开进程面板，然后选择进程和操作。额外操作放在 **More actions…**，参数逐项填写。前端不会把菜单选择拼成 slash 文本发送。

一级入口来自后端 ActionCatalog，覆盖目前全部 44 个内置操作：

| 菜单 | 操作 |
| --- | --- |
| `/model`、`/mode` | 模型配置、主/子 Agent 模型与默认值；当前模式和模式切换 |
| `/thinking` | 上轮 reasoning、显示方式、reasoning effort |
| `/approval` | 审批策略及会话授权管理 |
| `/mcp`、`/skills` | 列表、状态及后端提供的管理操作 |
| `/agents` | 子 Agent 列表、结果、消息、恢复、取消、清理 |
| `/ps` | 后台进程、轮询、打断、终止、密文输入 |
| `/session`、`/save`、`/new` | 浏览、恢复、保存、新建会话 |
| `/reset`、`/compact`、`/quit` | 清空、压缩、保存退出 |

`/compact` 直接打开策略面板：默认选中旧对话摘要，也可选择精简工具输出或深度压缩；只有深度压缩需要额外确认。面板显示后端估计的上下文用量，执行后报告前后变化。CLI 复用同一组选项；`/compact force <snip|summarize|collapse>` 仍可直接指定策略。

`/ps` 按命令选择进程，显示状态、耗时和本地/远端来源；已结束进程放在二级列表。进入详情自动读取输出，↑↓ 选择操作、Enter 执行、PgUp/PgDn 滚动、Home/End 查看进程信息或最新输出。刷新、打断和密文输入后仍留在详情页，终止进程树需要确认；Esc 只关闭面板。人工输出保留每个流最近 8,000 字符，读取游标独立于模型，重新进入详情不会丢掉已读输出。CLI 的显式 `/ps poll|interrupt|terminate <id>` 和 `/stop <id|all>` 仍可用。
| `/help`、`/config`、`/tokens`、`/status`、`/debug` | 帮助、配置、token、性能与调试 |

`View all details` 展示视图的全部字段。命令帮助里保留的旧 CLI 语法是后端参考信息；新版输入区的 slash 用于打开一级菜单。

| 内容/交互 | 新版位置 |
| --- | --- |
| assistant 流式输出、Markdown、代码、表格 | 会话记录；生成结束后格式化 |
| reasoning、工具参数、滚动输出、最终结果 | 会话记录；F4 展开保留的完整内容 |
| diff、stdout/stderr、诊断、耗时、退出码、截断说明、归档位置与校验值 | 工具记录展开视图；模型输出截断不会截断界面数据 |
| 已批准的相同 diff | 完成后折叠为执行摘要，完整 diff 仍可展开 |
| 计划、进度、子 Agent、后台进程、诊断、启动信息、状态和排队输入 | 顶部摘要及 F2 会话详情；对应命令面板提供操作 |
| 确认、单选、文本、密文输入 | 输入区上方的交互面板，聊天草稿保留 |
| 工具审批、会话授权范围、拒绝反馈、队列和超时 | 审批面板；键盘分页查看长 diff |
| 运行时追加指令、中断、命令排队、会话保存/恢复 | 后端处理；前端显示结果和状态 |
| 后端报错、断线 | 保留已有记录和草稿，显示错误；退出以失败状态返回 |

### 快捷键

审批区的“批准一次”使用灰青底色，拒绝使用红色，范围和反馈按键使用琥珀色。窄屏按完整操作项换行，翻页提示与位置放在面板标题右侧；普通菜单、表单和文档采用同一套按键引导色。

| 按键 | 功能 |
| --- | --- |
| Enter | 发送、进入菜单、确认选择 |
| Alt+Enter / Shift+Enter | 换行；Shift+Enter 取决于终端是否发送独立键码 |
| `/` / Ctrl+P | 命令菜单；Ctrl+P 保留草稿 |
| Esc | 返回、关闭菜单或取消交互 |
| ↑↓ | 菜单选择；输入为空时滚动会话，否则浏览输入历史 |
| Alt+↑↓ | 浏览输入历史，包括空输入时 |
| PgUp / PgDn | 滚动当前内容；长菜单翻页 |
| Home / End | 空输入时跳到会话开头 / 跟随最新输出；编辑时移动光标 |
| F1 / Ctrl+G | 快捷键帮助 |
| F2 / Ctrl+O | 会话、计划、进程、子 Agent 和启动详情 |
| F4 / Ctrl+R | 切换整个会话记录的详细视图：工具参数/完整输出、reasoning、LSP 诊断 |
| Ctrl+A/E、Ctrl+U/K/W | 移动光标及删除文本 |
| Ctrl+C | 依次取消交互、关闭菜单、清空草稿、中断运行、确认退出；停止期间再次按下退出 |
| Ctrl+D | 空草稿时保存退出 |

F4 作用于当前会话中所有已保留的记录：展开工具调用参数、完整收到的 stdout/stderr、diff、诊断与归档信息；显示模型已返回的 reasoning。再按一次恢复工具摘要和折叠视图。展开后可以用 PgUp/PgDn 查看前面的内容。快捷键说明统一放在输入栏下方，顶部显示当前是否处于详细视图。

紧凑视图把连续工具调用合为一组，显示数量和最新工具的一行摘要；读取类使用后端提供的行数、字符数或匹配数，不显示正文。shell 执行时仅预览最后 3 行，结束后收起。并行调用优先显示仍在运行的工具，失败摘要持续保留。模型的可见说明会分隔工具组，隐藏的 reasoning 不占位也不打断分组。F4 按原始顺序展开全部记录，折叠不会删除内容。

LSP 诊断默认显示文件名和各严重级别的数量，错误、警告使用对应主题色；F4 展开完整诊断的位置、消息和代码，避免大量诊断占满正文。

Markdown 表格根据视口宽度分列，单元格内部换行，并保留 Markdown 指定的左右/居中对齐。每列不足 8 格时改为逐条字段展示。长单元格默认显示 3 行，`… +N` 表示另有 N 行；F4 展开全文，原始消息不会被裁剪。历史分块携带表头，跨块保持列宽和对齐；表格行不从单元格中间切断，只布局可见块并复用滚动缓存。`npm run benchmark` 包含一万行长单元格表格的紧凑/展开滚动场景。

会话滚动到底部后自动恢复跟随最新输出，History 提示消失，底部恢复常规快捷键。

方向键、滚轮和翻页使用约 120ms 的整行过渡。连续输入合并目标，反向输入丢弃尚未完成的反向行程；Home / End 立即跳转，退出面板、缩放和切换展开状态会停止旧过渡。菜单选择仍立即响应。

F2 会话详情内按 `h` 浏览完整历史：`n/p` 翻页、`/` 搜索、方向键选择、Enter 读取原文；`m` 切换消息与原始事件，`a` 读取归档，`r` 刷新。历史经 JSON-RPC 按需加载，与模型的历史工具共用后端查询；关闭后不会在主页留下占位内容。

展开的 reasoning 使用 Markdown 渲染，保留标题、列表、代码和主题配色，整体降低亮度，与正式回答区分。

命令面板和详情页只在交互区域显示，关闭后不在主会话中留下副本或菜单选择记录。实际执行结果与错误提示仍保留在会话中。

输出区底部的动态状态行显示等待模型响应、思考中、输出中或正在执行的工具，并显示当前阶段用时；折叠 reasoning 和工具输出时仍可见。等待确认时改成静态输入提示，任务结束后自动消失，不写入会话历史。动画只刷新状态行，不重新排版历史内容。

光条、转圈亮度、面板渐入和启动 logo 共用 Ink 的本地动画调度，目标 60fps；位置和亮度由实际经过时间计算，掉帧不会拖慢动画节奏。转圈仍每秒转一轮。启动 logo 在约 1.9 秒后缓动收起，消失边缘按主题背景渐隐，整行释放空间时保持输入栏位置；命名 ANSI 主题使用离散亮度。Ink 刷新上限为 120fps，为渲染和定时器调度留出余量；这不改变后端事件频率，渲染性能上报单独限制为每秒一次。等待用户输入、静止或有限动画结束后停止计时。

滚动按键先立即移动一行，再缓动到目标位置；后续滚动帧直接通知视图，不再经过后端事件使用的 16ms 合并窗口。连续输入累计距离，反向、End 和切换面板会取消旧目标。滚动时复用未变化的侧栏和文本区域，重要运行状态仍正常更新。

排队中的 Prompt 和 Command 在输入栏上方的 `QUEUED` 区域显示内容预览；F2 会话详情可查看完整文本及全部队列。队列由后端快照驱动，执行或取消后自动移除，队列清空后隐藏整个区域。

审批：`y` / Enter 批准一次，`n` 拒绝，`s` 选择会话授权范围，`f` 输入拒绝反馈。单选支持数字 1–9。

支持 bracketed paste、中文和组合 emoji。密文输入仅显示圆点，不进入前端输入历史；历史保存在本地工作目录 `.rcoder/tui-history.jsonl`。终端原生选择/复制仍可用；支持 alternate scroll 的终端可通过滚轮滚动空输入状态下的会话。`--no-alt-screen` 使用主终端缓冲区。

## 远端后端

`--backend` 启动任意提供相同 stdio 协议的程序，`--` 后的参数原样交给它。例如用 SSH 承载协议：

```sh
node reuleauxcoder-tui/dist/cli.js --backend ssh -- -T devbox \
  'cd /work/project && exec /opt/ReuleauxCoder/.venv/bin/python -m reuleauxcoder --rpc-stdio'
```

在 VS Code Remote 的终端内直接启动时，前后端都在远端工作区运行。也可以在本机启动 TUI，通过 SSH 子进程连接远端。输入历史属于前端本地目录，会话文件和工具文件属于后端工作区。

一条连接拥有一个后端进程。关闭前端会要求后端中断、保存并退出；断线后可通过 `--resume` 恢复已保存会话。目前没有驻留服务、自动重连或多客户端共享会话。Go 工具执行 peer 的 relay 协议独立于这条 UI 连接。

## 结构与验证

```text
src/cli.tsx          启动参数、子进程和终端生命周期
src/protocol/        JSON 编解码、双向 peer、运行时客户端
src/state/           会话记录、输入编辑、历史、菜单与交互状态
src/ui/              React 布局、内容窗口、面板与格式化
test/                Python 实际运行时、跨语言协议、Ink 和 PTY 验证
```

后端命令参数 dataclass 是表单字段的单一来源；`preview` 显式声明安全的菜单预览。命令面板由对应 Python 命令模块构建。前端只拥有选择、过滤、草稿、折叠和滚动状态。

实时记录内容保留在会话状态中，只有当前可见文本块进入排版和 React；Markdown/折行缓存最多保留 6,000 行。未访问部分使用高度估计，滚动提示中的 `~` 表示总行数尚未全部测量。记录锚点保持阅读位置，改变宽度和流式追加不会重新排版全部历史。事件与历史回复携带会话代数，恢复历史与后到的快照不会互相覆盖。JSON-RPC 返回错误和异常断线会解除挂起请求。详见 [统一历史查询](../docs/history-query.md)。

RPC 接收只扫描新到达的分片，在一条完整消息到齐后合并一次，并继续执行 16 MiB 的单条消息大小限制。

shell 实时折叠预览随新输出增量更新，显示末尾最多三行。预览缓存最多保留 8,192 个 UTF-16 代码单元的正文及同样上限的待定尾部空白，超长行保留末尾；跨事件的 ANSI 控制序列会被清理。完整输出仍由会话记录保留，F4 可展开查看。

长回复和推理的追加事件只重新切分末尾文本块；已完成的前缀保持不动，完成、替换和折叠切换仍正常失效。运行快照只在字段发生变化时递增 revision。支持 `conditional_snapshots` 的后端接受 `runtime.snapshot` 的 `known_revision`，未变化时返回 null；旧后端继续使用完整快照。TUI 忙碌或等待交互时每 500ms 补查状态，空闲时改为每 5 秒；事件推送保持实时，未变化的 Git 快照不触发重绘。

```sh
npm --prefix reuleauxcoder-tui run check
npm --prefix reuleauxcoder-tui test
```

### 渲染 benchmark

```sh
npm --prefix reuleauxcoder-tui run benchmark -- --label local --json /tmp/rcoder-render.json
```

固定生成一万条消息、240 万字符单条消息、最高 500 万字符流式回复和 5,000 行详情，在 160×40 的模拟终端上运行真实 Ink + App。仅空闲场景启动真实 Python 测试后端，不连接模型或外部服务。报告分开记录首次排版、追加与滚动排版、实际文本输出帧间隔、输入延迟、Ink 渲染耗时、单核 CPU 占比和输出带宽。输入延迟从 controller 接收翻页开始，到下一次文本写出为止；忙碌场景也可能由动画先写出，不等同于实际终端的按键到画面延迟。

启动动画不计入稳定场景；普通面板每 160ms 收到一次翻页，平均输出帧率包含静止间隔，连续面板和主会话滚动则每 40ms 输入一次。空闲测量持续 6.5 秒，覆盖一次 5 秒状态补查；其余场景持续 2.5 秒。Ink 回调耗时不含 React 更新和面板格式化，需结合整帧间隔与纯排版测量阅读。数值中的 0 表示低于两位小数精度。

当前面板缓存只保留该面板的一份折行结果；宽度或内容改变后重新计算，关闭或切换面板即释放。正文继续使用可见块排版和 6,000 行缓存上限。已经定宽的行不再重复水平裁剪；同一文本区域合并 React 节点，未变化的正文行复用渲染结果。

[2026-09-11 的前后测量](benchmarks/render-2026-09-11.json) 在同一台 Linux / Xeon 9470C / Node 24.16.0 上记录。基线已经包含新动画与滚动，尚未加入面板缓存、节点合并和裁剪优化，Ink 上限为 75fps；优化后上限为 120fps，本地动画目标均为 60fps。

| 场景 / 指标 | 优化前 | 优化后 |
| --- | ---: | ---: |
| 5,000 行面板滚动排版 P95 | 61.91ms | 0.01ms |
| 完整界面忙碌动画输出 | 37.60fps | 59.85fps |
| 忙碌动画 Ink 渲染 P95 | 11.22ms | 7.56ms |
| 面板翻页场景单核 CPU | 87.02% | 47.27% |

这是本地单次对比，终端绘制、SSH 延迟及真实后端流量不在测量范围内；不把硬件相关耗时写成 CI 通过门槛。测试检查排版复用、失效、阅读位置和生命周期等行为。

[本轮滚动与空闲优化记录](benchmarks/scroll-2026-09-11.json) 保留两组基线：`before_all` 是上一轮提交 `1b64f0c`；`before_scroll_scheduling` 已包含其他优化，仍有滚动帧的 16ms 额外等待。下表的滚动对比使用后者，流式追加使用前者。

| 场景 / 指标 | 优化前 | 优化后 |
| --- | ---: | ---: |
| 连续面板滚动输出 | 33.92fps | 60.38fps |
| 连续面板滚动帧间隔 P95 | 32.64ms | 20.90ms |
| 普通翻页输入到输出 P95 | 54.80ms | 12.72ms |
| 500 万字符回复追加与可见排版 P95 | 18.10ms | 10.58ms |
| 连续面板滚动单核 CPU | 45.33% | 70.41% |

新增的一万条消息主会话连续滚动测得 65.10fps、输入到输出 P95 19.18ms；同时显示忙碌动画时为 61.14fps。即时首行更新与动画帧可能使总输出略高于动画目标 60fps。此处以更多 CPU 和终端输出换取响应速度，不代表每帧都能稳定低于 16.7ms。终端窗口越大、SSH 越慢，实际表现越可能受绘制和带宽限制。

无活动 goal 的空闲 RPC 场景从每秒两次状态重绘降为零；6.5 秒内只收到一次 41 字节的条件查询回复。500 万字符追加仍有 JavaScript 字符串拼接/切片成本，本轮仅消除了对完整前缀的重复分块扫描。

测试使用真实 Python CommandService、RuntimeServer、codec 和 stdio，替换 LLM 循环以避免外部模型调用。涵盖全部命令目录和预览入口、参数表单、四类反向交互、工具完整信息、队列/中断、失败保存、恢复会话、Unicode、密文遮盖，以及实际 PTY 中的缩放和终端恢复。PTY 测试在 Windows 跳过。用 `RCODER_TUI_PYTHON` 指定测试 Python 环境；它必须安装本仓库 Python 包及依赖。

Ink 7 的 `useInput` 不暴露 F1、F2、F4，适配集中在 `src/ui/terminal.ts`；升级锁定的 Ink 版本时运行终端测试。Ctrl+G/O/R 同时提供替代键位。
