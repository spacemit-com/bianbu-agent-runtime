# Tools 代码目录

Runtime 自带工具是普通 Python 模块，随 wheel / Debian 包一起安装。
用户目录 `~/.bianbu-agent/tools/` 只存放额外安装的工具，不再承载内置实现。

| 目录 / 文件 | 职责 |
| --- | --- |
| `files/` | `read_file`、`write_file`、`search_files`、`patch` |
| `processes/` | `terminal`、`process`；`launch.py` 启动后台进程，`records.py` 读取及校验进程记录；`manager.py` 等提供宿主进程服务 |
| `browser/` | 九个 `browser_*` 工具；所有命令共享 `client.py`；`manager.py` 提供宿主浏览器服务 |
| `web/` | `web_search`、`web_extract`，以及搜索、提取和 URL 校验辅助代码 |
| `skills/` | `skills_list`、`skill_view`、`skill_manage` |
| `kanban/` | 十二个 `kanban_*` 工具和 `todo` |
| `builtins.py` | 内置工具 ID → 模块的显式注册表、源码检查和加载入口 |
| `catalog.py` | 合并内置注册表与用户工具目录，生成固定的能力快照 |
| `loader.py`、`manager.py` | 用户 / 插件工具目录的验证、加载、安装和卸载；共享工厂到 ToolContribution 的适配逻辑 |
| `runtime.py`、`runtime_services.py`、`state.py`、`kanban_state.py` | 调用上下文、宿主服务及状态管理 |

## 修改或增加内置工具

1. 在对应职责目录添加 `<tool_id>.py`，导出 `create_tool(context)`。
2. 工厂返回由 `bianbu_agent.tools.tool` 装饰的函数；工具 ID、说明和 schema 从函数派生。
3. 在 `builtins.py` 的 `BUILTIN_MODULES` 注册 ID 和模块路径。
4. 补充行为测试；代码更新后重启 Runtime Worker，使新的模块进入进程。

Control 只读取注册表和 Python docstring，不执行工具工厂。Worker 直接导入内置模块。
同组辅助模块计入源码摘要；不需要将源码复制到用户目录，也没有独立的内置工具 requirements 文件。
内置依赖由项目 `pyproject.toml` 声明（Web 工具使用项目已有的 httpx）。

工具数据写到调用上下文的 workspace / state / data 目录，不能写入安装代码目录。
技能管理使用 Runtime home 下的 `skills/`；技能读取同时保留可信用户技能挂载路径。

## 用户扩展与升级边界

外置工具继续使用 `<runtime-home>/tools/<tool_id>/tool.py` 和可选 `requirements.txt`，
由 `DirectoryToolLoader` 加载。插件和专家自带工具保留原来的包目录和限定 ID。
内置 ID 属于 Runtime；工具安装管理器拒绝使用这些 ID。遗留同名目录不会覆盖内置模块，
也不会在扫描时执行它们。用户需要不同实现时，应给扩展使用独立工具 ID。

Debian 默认 profile 不再包含 tools。升级时，provisioner 将旧 `.managed-profile.json`
登记的工具副本备份到 `backups/package-upgrade/<digest>/tools/` 后移除；未登记的用户工具保留。
内置工具随 Runtime 包升级，不能通过外置工具卸载接口删除。
