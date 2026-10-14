"""UI-facing, bilingual one-line summaries for the built-in tools.

Tool docstrings are the model-facing contract: they are long, they carry
``Args:`` sections, and each author writes them in a single language.  The
Console and the Bibit settings plugin instead need a short intro that follows
the deployed language, so every Runtime-owned tool declares its summary in both
languages here.  Tools Runtime does not own (user tools, Plugin tools, MCP
tools) have no entry and the frontends keep showing the tool's own description.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType


#: Languages every summary must provide, in UI display order.
SUMMARY_LANGUAGES = ("zh", "en")

TOOL_SUMMARIES: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "browser_back": {
            "zh": "返回浏览器历史中的上一页（需先调用 browser_navigate）。",
            "en": "Go back to the previous page in browser history (call browser_navigate first).",
        },
        "browser_click": {
            "zh": "点击快照里的元素（用 ref ID，例如 @e5）。",
            "en": "Click an element by its snapshot ref ID (for example @e5).",
        },
        "browser_console": {
            "zh": "读取浏览器控制台日志与 JS 报错，也可执行 JS 表达式取页面数据。",
            "en": "Read console output and JS errors, or evaluate a JS expression to inspect the page.",
        },
        "browser_get_images": {
            "zh": "列出当前页面的所有图片及其 URL 和替代文本。",
            "en": "List every image on the current page with its URL and alt text.",
        },
        "browser_navigate": {
            "zh": "打开一个 URL 并初始化浏览器会话，是其它 browser 工具的前置。",
            "en": "Open a URL and initialize the browser session; required before the other browser tools.",
        },
        "browser_press": {
            "zh": "按下键盘按键（Enter、Tab、方向键或快捷键）。",
            "en": "Press a keyboard key (Enter, Tab, arrow keys or a shortcut).",
        },
        "browser_scroll": {
            "zh": "按方向滚动页面，用于查看视口之外的更多内容。",
            "en": "Scroll the page in a direction to reveal content outside the viewport.",
        },
        "browser_snapshot": {
            "zh": "获取当前页面的可访问性树文本快照（含可交互元素与 ref ID）。",
            "en": "Get a text snapshot of the page accessibility tree with interactive elements and ref IDs.",
        },
        "browser_type": {
            "zh": "向快照中的输入框（ref ID）输入文本，会先清空原内容。",
            "en": "Type text into an input field by ref ID, clearing its current content first.",
        },
        "kanban_attach": {
            "zh": "把 Base64 编码的文件作为附件保存到 Kanban 任务。",
            "en": "Save a Base64-encoded file as an attachment on a Kanban task.",
        },
        "kanban_attach_url": {
            "zh": "从安全的公开 HTTP(S) URL 下载文件并附加到 Kanban 任务。",
            "en": "Download a file from a safe public HTTP(S) URL and attach it to a Kanban task.",
        },
        "kanban_attachments": {
            "zh": "列出 Kanban 任务已保存附件的元数据和本地路径。",
            "en": "List the attachment metadata and local paths saved on a Kanban task.",
        },
        "kanban_block": {
            "zh": "记录真实阻塞原因，并把 Kanban 任务转为阻塞状态。",
            "en": "Record the real blocker and move a Kanban task to the blocked state.",
        },
        "kanban_comment": {
            "zh": "向 Kanban 任务追加一条持久保存的 Markdown 评论。",
            "en": "Append a persistent Markdown comment to a Kanban task.",
        },
        "kanban_complete": {
            "zh": "把 Kanban 任务标记为完成，并保存结构化交接摘要与产物信息。",
            "en": "Mark a Kanban task complete with a structured handoff summary and artifacts.",
        },
        "kanban_create": {
            "zh": "创建持久化 Kanban 任务（负责人、父任务、优先级、幂等键），就绪后由 Dispatcher 启动 Worker。",
            "en": "Create a persistent Kanban task (assignee, parent, priority, idempotency key); the dispatcher starts a worker once it is ready.",
        },
        "kanban_heartbeat": {
            "zh": "为长时间运行的 Kanban 任务记录存活心跳和简短进度。",
            "en": "Record a liveness heartbeat and short progress note for a long-running Kanban task.",
        },
        "kanban_link": {
            "zh": "在两项现有 Kanban 任务之间建立父任务到子任务的依赖。",
            "en": "Link two existing Kanban tasks as a parent-to-child dependency.",
        },
        "kanban_list": {
            "zh": "按负责人、状态或项目命名空间列出持久化 Kanban 任务。",
            "en": "List persistent Kanban tasks by assignee, status or project namespace.",
        },
        "kanban_show": {
            "zh": "读取一项 Kanban 任务的完整状态、依赖、评论、运行记录和最近事件。",
            "en": "Read one Kanban task's full state, dependencies, comments, runs and recent events.",
        },
        "kanban_unblock": {
            "zh": "把阻塞中的 Kanban 任务恢复为可继续处理状态。",
            "en": "Move a blocked Kanban task back to a workable state.",
        },
        "patch": {
            "zh": "在文件里做精确查找替换（模糊匹配，替代 terminal 里的 sed/awk）。",
            "en": "Make targeted find-and-replace edits in files with fuzzy matching; use it instead of sed/awk.",
        },
        "process": {
            "zh": "管理 terminal(background=true) 启动的后台进程：列出、轮询、看日志、结束。",
            "en": "Manage background processes started by terminal(background=true): list, poll, show logs or kill.",
        },
        "read_file": {
            "zh": "读取当前 Tool 工作区里的 UTF-8 文本文件。",
            "en": "Read a UTF-8 text file from the current Tool workspace.",
        },
        "read_image": {
            "zh": "用视觉模型读取工作区里的图片并回答关于图片的问题。",
            "en": "Read a workspace image with a vision model and answer a question about it.",
        },
        "search_files": {
            "zh": "按内容或文件名搜索文件（ripgrep 实现，替代 terminal 里的 grep/rg/find/ls）。",
            "en": "Search file contents or find files by name (ripgrep-backed; use it instead of grep/rg/find/ls).",
        },
        "skill_manage": {
            "zh": "管理技能：创建、更新、删除。",
            "en": "Manage skills: create, update or delete them.",
        },
        "skill_view": {
            "zh": "加载某个技能的完整内容，或读取它附带的脚本与模板。",
            "en": "Load one skill's full content, or read the scripts and templates it ships.",
        },
        "skills_list": {
            "zh": "列出可用技能的名称和简介（用 skill_view 查看完整内容）。",
            "en": "List available skills with their short descriptions; use skill_view for the full content.",
        },
        "terminal": {
            "zh": "在当前 Tool 工作区里执行一条 shell 命令。",
            "en": "Run a shell command inside the current Tool workspace.",
        },
        "todo": {
            "zh": "发布当前会话的完整任务清单，适合 3 步以上的复杂任务。",
            "en": "Publish the complete task list for this session; use it for tasks with 3+ steps.",
        },
        "web_extract": {
            "zh": "提取公开网页的正文内容（可限制返回字符数）。",
            "en": "Extract the main content of public web pages, with an optional character limit.",
        },
        "web_search": {
            "zh": "搜索网页；查时事用 category=news。",
            "en": "Search the web; use category=news for current events.",
        },
        "write_file": {
            "zh": "在当前 Tool 工作区里原子写入 UTF-8 文本文件。",
            "en": "Atomically write a UTF-8 text file inside the current Tool workspace.",
        },
    }
)


def tool_summaries(name: str) -> Mapping[str, str]:
    """Return the localized UI summaries of one built-in tool."""

    return TOOL_SUMMARIES.get(name, {})


__all__ = ["SUMMARY_LANGUAGES", "TOOL_SUMMARIES", "tool_summaries"]
