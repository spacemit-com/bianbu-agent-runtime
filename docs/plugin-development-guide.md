# Bianbu Agent 插件开发

Plugin 是 Runtime 的领域扩展单位，可贡献 Hook、Tool、Skill 和 MCP。

## 目录结构

```text
plugins/example.plugin/
├── plugin.yaml
├── hooks/<hook-name>/hook.json
├── scripts/<handler>.py
├── tools/<tool-name>/tool.py
├── skills/<skill-name>/SKILL.md
├── mcps/<mcp-name>/mcp.json
└── lib/
```

目录名就是插件 ID：小写标识符，建议使用点号命名空间（`vendor.bundle`），单个小写
单词（`superpowers`）同样合法。`plugin.yaml` 只声明身份（`name`/`version`/
`description`，可选 `dependencies`/`config_schema`/`entrypoint`）；提供什么能力由包内
`tools/`、`skills/`、`hooks/`、`mcps/` 决定，清单里不写贡献，也没有权限
字段。Hook 仅通过外部命令 JSON
协议加载，每个 Hook 目录只包含 `hook.json`，业务脚本放在 `scripts/`。不支持旧 Python
工厂入口。Tool 和 Skill 使用 `<plugin-id>/<name>` 限定名；每次 Run 租用不可变
`PluginGeneration`，热更新只影响新 Run。

## 市场插件包

契约见 `plugin.yaml` 契约规范：Runtime 只认 `plugin.yaml`。

Agent Power Manager 等市场工具把同一份包投放到多个 Agent 主目录时，包内必须带本契约的
`plugin.yaml`（`name`、`version`，`v` 前缀要规范化成 PEP 440），并按契约去掉其他 Agent
的清单与元数据（`apc-plugin.json`、`.claude-plugin/`、`.codex-plugin/`、
`.hermes-plugin/`、`__init__.py`）：Runtime 不读它们，留在包里会被当作
`unsupported Plugin package entry` 拒绝。包内 `tools/`、`skills/`、`hooks/`、`mcps/`
有什么，插件就提供什么。

无法解析的插件目录会被跳过：Runtime 记录原因，其余插件（含系统插件）继续
可用；`<data_dir>/mcps/<id>/mcp.json` 无效时同样跳过该 MCP 而不影响其他条目。
符号链接、越界路径等可被重定向的条目仍会使整次读取失败，不会被静默跳过。

## Hook 与 Tool 的调用身份

Runtime 在 Hook 的 stdin JSON 和 Tool 的 `ToolCallContext` 中同时提供：

- `account_id`：当前 Runtime 账号；尚未接入系统账号时为 `"default"`；
- `agent_id`：本次 Run 实际选择的 Agent ID。

Hook 脚本读取 JSON 的 `account_id` 和 `agent_id`；直接贡献的 Tool handler 使用
`call.account_id` 和 `call.agent_id`；目录 Tool 在被装饰函数执行期间使用
`package_context.call.account_id` 和 `package_context.call.agent_id`。不要把这些宿主身份
声明成模型可填写的 Tool 参数。

完整示例、加载期与调用期上下文的区别，参见扩展接入指南：Runtime 调用身份。

Hook 配置、`RUNTIME_PYTHON`、权限、取消和私有内存 `state` 见命令 Hook 协议。

## 成员加载

Tool、Skill 与 MCP 由宿主加载；Hook 在 Worker 的外部子进程运行并继承其安全边界。
Runtime 不再提供 Plugin Console（自描述管理接口）：插件若要暴露状态，应通过自己的
MCP server 或 Runtime 的只读资源表达。`context.data_root` 是插件私有数据目录，
`context.services` 只包含宿主显式提供的窄服务。

插件不是独立的操作系统沙箱，只应安装可信代码。凭据应写入插件私有数据或
权限受限的提供商配置，不得写入日志和 API 响应。

## Plugin 贡献 MCP

一个 Plugin 可以在 `mcps/<name>/mcp.json` 贡献多个 MCP Server。其公开 ID 是
`<plugin-id>/<name>`。Runtime 启动或热更新时连接 Server 并执行工具发现。远端名称中的
连字符和其他不安全字符会规范化为小写下划线；协议调用仍使用原始远端名称。

普通用户可以在 Web「MCP」页面填写显示名称和一行 stdio 启动命令直接安装。例如：

```text
npx -y @modelcontextprotocol/server-filesystem /data/projects
```

控制台会把独立 MCP 保存到 `<data_dir>/mcps/<mcp-id>/mcp.json` 并热更新 Runtime。命令使用 POSIX 参数规则解析，
不会交给 Shell；换行、管道、重定向、命令连接和命令替换会被拒绝。页面同时提供状态刷新
和卸载操作。Filesystem MCP 应只开放完成任务必需的目录。

高级配置支持两种 transport：

```json
{
  "transport": "stdio",
  "command": "uvx",
  "args": ["mcp-server-example", "${workspace}"],
  "env": {"API_TOKEN": "${API_TOKEN}"},
  "timeout_seconds": 30,
  "startup_timeout_seconds": 120
}
```

```json
{
  "transport": "streamable-http",
  "url": "https://example.com/mcp",
  "headers": {"Authorization": "Bearer ${API_TOKEN}"},
  "timeout_seconds": 30
}
```

允许字段是 `transport`、对应 transport 的连接字段和 `timeout_seconds`、
`startup_timeout_seconds`；未知字段会在安装阶段被拒绝。
`timeout_seconds` 是单次 MCP 调用的预算，`startup_timeout_seconds` 是启动进程与握手
的预算，缺省等于 `timeout_seconds`；用 `npx` 这类启动时才解析依赖的命令时应该把它调大
（`mcp add` 写出的声明就是 120s）。`${workspace}` 是 Runtime workspace；`${NAME}` 是启动服务时提供的环境
变量。独立 MCP Server 用 CLI 通过启动命令安装：

```bash
uv run bianbu-agent --config ~/.bianbu-agent/config.yaml \
  mcp add example --command /absolute/path/to/example-server --args --flag
```

MCP 在 Agent 中按整台 Server 选择，不能选择其内部单个 Tool。Plugin 贡献的 MCP 跟随
Plugin 生命周期，MCP 页面会显示归属，但不能单独卸载。

CLI 安装需重启服务。Web `/api/v1/mcp-servers` 命令安装和
`/api/v1/plugins` 目录安装都会构建并切换新的
不可变 Plugin Generation；连接或发现失败时，当前 Generation 继续服务。管理 API 返回
显示名称、非敏感启动命令、transport、健康状态和远端工具，不返回 command 环境、headers
或已解析凭据。
