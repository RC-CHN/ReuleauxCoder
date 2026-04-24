"""
ReuleauxCoder TUI Mockup
这是一个独立的 TUI 界面占位设计，用于展示 ReuleauxCoder 的交互布局。
运行前需要安装 textual 依赖：
pip install textual
"""

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Header,
    Footer,
    Static,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    Tree,
)
from textual.binding import Binding


class ChatMessage(Static):
    def __init__(self, role: str, content: str, **kwargs):
        super().__init__(**kwargs)
        self.role = role
        self.content = content

    def compose(self) -> ComposeResult:
        # 根据角色显示不同的标识
        yield Label(f"[{self.role.upper()}]", classes=f"role-{self.role}")
        # 使用 Markdown 渲染聊天内容
        yield Markdown(self.content)


class ReuleauxTUI(App):
    TITLE = "ReuleauxCoder"
    SUB_TITLE = "Terminal AI Coding Assistant"

    # 样式定义
    CSS = """
    Screen {
        layout: vertical;
    }
    
    #main-container {
        layout: horizontal;
        height: 1fr;
    }
    
    /* 左侧边栏：历史会话 */
    #sidebar {
        width: 30;
        dock: left;
        border-right: solid $primary;
        height: 1fr;
        padding: 1;
        background: $surface;
    }
    
    /* 中间聊天区域 */
    #chat-area {
        width: 1fr;
        height: 1fr;
        layout: vertical;
    }
    
    #messages {
        height: 1fr;
        overflow-y: auto;
        padding: 1 2;
    }
    
    /* 底部输入框 */
    #input-area {
        height: 3;
        dock: bottom;
        border-top: solid $primary;
        padding: 0 1;
        layout: horizontal;
        background: $surface;
    }
    
    #prompt-input {
        width: 1fr;
        border: none;
    }
    
    /* 右侧面板：上下文/状态 */
    #context-panel {
        width: 35;
        dock: right;
        border-left: solid $primary;
        height: 1fr;
        padding: 1;
        background: $surface;
    }
    
    .panel-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    
    ChatMessage {
        margin-bottom: 1;
        padding: 1;
        border-left: solid $background;
    }
    
    ChatMessage:hover {
        border-left: solid $accent;
    }
    
    .role-user {
        color: $success;
        text-style: bold;
    }
    
    .role-assistant {
        color: $secondary;
        text-style: bold;
    }
    
    .role-system {
        color: $warning;
        text-style: bold;
    }
    """

    # 快捷键绑定
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("ctrl+b", "toggle_sidebar", "Toggle Sidebar", show=True),
        Binding("ctrl+p", "toggle_context", "Toggle Context", show=True),
    ]

    def compose(self) -> ComposeResult:
        # 顶部 Header (显示标题和时钟)
        yield Header(show_clock=True)

        with Container(id="main-container"):
            # 1. 左侧边栏：历史会话列表
            with Vertical(id="sidebar"):
                yield Label("📚 Sessions", classes="panel-title")
                yield ListView(
                    ListItem(Label("1. 新的 TUI 界面设计")),
                    ListItem(Label("2. 修复 loop.py 中的 bug")),
                    ListItem(Label("3. 重构 hooks 系统")),
                    ListItem(Label("4. 为 MCP 编写测试")),
                )

            # 2. 中间：主聊天区域
            with Vertical(id="chat-area"):
                with VerticalScroll(id="messages"):
                    yield ChatMessage(
                        "system",
                        "ReuleauxCoder Initialized.\nWorkspace: `/home/pan/proj/ReuleauxCoder`\nMode: `Code`",
                    )
                    yield ChatMessage(
                        "user",
                        "写一个独立的tui界面样式设计给我看？参考我们这个项目，只做个占位就好，不需要实际链接",
                    )
                    yield ChatMessage(
                        "assistant",
                        "好的！我使用 `Textual` 框架为您设计了一个 TUI 界面。\n\n这个界面包含：\n1. **左侧边栏**：显示历史会话记录。\n2. **中间主区域**：显示与 AI 的对话（支持 Markdown 渲染）以及底部的输入框。\n3. **右侧边栏**：显示当前上下文状态（使用的模型、当前活动文件树等）。",
                    )
                    yield ChatMessage(
                        "system",
                        "⚙️ Tool Execution: `write_to_file`\nPath: `reuleauxcoder/interfaces/tui/mockup.py`",
                    )
                    yield ChatMessage(
                        "assistant",
                        "我已经创建了 TUI 占位文件。您可以直接运行它来预览界面效果。",
                    )

                # 底部输入区
                with Horizontal(id="input-area"):
                    yield Input(
                        placeholder="Ask ReuleauxCoder anything... (Press Enter to send)",
                        id="prompt-input",
                    )

            # 3. 右侧面板：上下文状态与工具
            with Vertical(id="context-panel"):
                yield Label("💡 Context", classes="panel-title")
                yield Static(
                    "Model: gemini-3.1-pro-preview\nMode: Code\n\nTokens: 1250 / 128000\nCost: $0.00"
                )
                yield Static("-" * 30)

                yield Label("📂 Active Files", classes="panel-title")
                tree = Tree("Workspace")
                tree.root.expand()
                rc_node = tree.root.add("reuleauxcoder", expand=True)
                interfaces_node = rc_node.add("interfaces", expand=True)
                tui_node = interfaces_node.add("tui", expand=True)
                tui_node.add("mockup.py")
                yield tree

        # 底部 Footer (显示快捷键)
        yield Footer()

    def action_toggle_sidebar(self) -> None:
        """切换左侧边栏显示状态"""
        sidebar = self.query_one("#sidebar")
        sidebar.display = not sidebar.display

    def action_toggle_context(self) -> None:
        """切换右侧面板显示状态"""
        context_panel = self.query_one("#context-panel")
        context_panel.display = not context_panel.display


if __name__ == "__main__":
    app = ReuleauxTUI()
    app.run()
