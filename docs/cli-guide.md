# bianbu-agent 使用指南

`bianbu-agent` 保留本地资源管理命令，以及 Runtime 服务启动需要的 `serve`、`worker` 和 `--role`。
资源命令直接操作配置指定的资源文件，不启动或连接 Runtime、Worker，不加载模型、执行工具代码或启动 MCP 进程。

## 帮助与配置

```bash
bianbu-agent
bianbu-agent --help
bianbu-agent --version
bianbu-agent agent --help
bianbu-agent agent create --help
```

不带参数只显示帮助。所有资源和子命令都支持 `--help`；帮助与版本查询不读取 Runtime 配置、不初始化资源目录。

全局参数放在资源名称之前：

```bash
bianbu-agent --config /home/bianbu/.bianbu-agent/config.yaml agent list
bianbu-agent --debug --config /home/bianbu/.bianbu-agent/config.yaml skill list
```

未指定 `--config` 时，依次选择当前目录的 `runtime.yaml`、环境变量 `BIANBU_AGENT_CONFIG`、
`~/.bianbu-agent/config.yaml`。配置中的相对 `data_dir` 按配置文件所在目录解析。
配置文件必须已经存在；可以直接创建最小配置：

```yaml
# ~/.bianbu-agent/config.yaml
data_dir: .
```

上例的数据目录就是 `~/.bianbu-agent/`。也可以复制已有部署配置后修改 `data_dir`。

## 资源管理速查

以下命令均放在 `bianbu-agent [--config 文件]` 后面。

| 资源 | 子命令 |
| --- | --- |
| `agent` | `list`、`create`、`show`、`verify`、`remove` |
| `skill` | `list`、`install`、`show`、`verify`、`remove` |
| `plugin` | 上述包命令，以及 `members`、`effective`、`rollback` |
| `mcp` | `list`、`add`、`get`、`remove`，以及目录包的 `install` |
| `model` | `list`、`create`、`show`、`remove`、`bootstrap-chat` |
| `session` | `list`、`create`、`show`、`remove` |

资源命令通常输出 JSON。操作失败会在标准错误输出原因；`--debug` 显示完整异常。

### Agent

```bash
bianbu-agent agent list
bianbu-agent agent create Writer --provider my-provider --model my-model
bianbu-agent agent show com.bianbu.实际ID
bianbu-agent agent verify com.bianbu.实际ID
bianbu-agent agent remove com.bianbu.实际ID
```

`create` 将模型选择写入 `agents/<ID>/agent.yaml`，生成 `com.bianbu.` 加十二位随机十六进制字符的 ID。
以返回的 ID 执行后续命令。可以重复传入 `--tool`、`--skill`、`--mcp`、`--plugin` 声明资源引用；
Hook 属于 Plugin，不需要也不可能在 Agent 上单独声明。
创建不调用模型、不启动专家。单个专家的查询和校验只检查该专家的文件，不执行旧数据迁移或预先扫描全部专家。

### Skill 与 Plugin

Tool 已经内置在 Runtime 代码里，不再支持安装：需要扩展能力时使用 MCP。

```bash
bianbu-agent skill install /路径/example.skill

bianbu-agent plugin install /路径/plugin包
bianbu-agent plugin list
bianbu-agent plugin show vendor.bundle
bianbu-agent plugin verify vendor.bundle
bianbu-agent plugin members
bianbu-agent plugin effective
bianbu-agent plugin install /路径/plugin新版本 --upgrade
bianbu-agent plugin rollback vendor.bundle
bianbu-agent plugin remove vendor.bundle
```

`skill` 和 `plugin` 同样支持 `list/show/verify/remove`；没有 `hook` 资源命令，
Hook 必须随 Plugin 安装，使用 `hooks/<id>/hook.json` 声明事件和命令，脚本放在
`scripts/`，详见命令 Hook 协议。

`plugin.yaml` 是插件唯一的清单，只写身份：`schema_version: 1`、`name`（小写标识符，
建议带命名空间点号，必须等于包目录名）、PEP 440 `version`、可选的 `description`/
`dependencies`/`config_schema`/`entrypoint`。插件提供什么能力由包内目录决定
（`tools/`、`skills/`、`hooks/`、`mcps/`），清单里不再有 `sdk_version`、
`permissions`、`contributions`。契约见 `plugin.yaml` 契约规范。
其他 Agent 的清单（`apc-plugin.json`、`.claude-plugin/` 等）不属于本契约，留在包内会被
拒绝。
无法解析的插件目录会被跳过并在标准错误输出原因，不会影响其他插件
（系统插件始终可见）；需要定位单个插件的问题时用 `plugin show <name>`。
MCP 目录遵循同样的规则：无法读取的 `<data_dir>/mcps/<id>/mcp.json` 会被跳过并记录，
不会让整个目录列表失败。
`install` 校验并复制本地文件；`remove` 按存储实现移除或移入恢复目录。
手工放到 `<data_dir>/skills/<name>/` 的 Skill 目录（含 `SKILL.md`）会被直接发现：
Runtime 的 Skill 根目录本就允许这种投放，所以它即使没有安装器写入的
`.bianbu-agent-sdk-ownership.json`，`list/show/verify/remove` 也按目录内容推导的记录
处理，不会为了管理而向该目录写回元数据；名字非法或 `SKILL.md` 无法解析的目录会被跳过。
`members` 读取插件成员目录；`effective` 合并配置默认值、Runtime 配置和数据目录的 `plugins.yaml`，不激活插件。
`rollback` 恢复升级前保留的版本；没有可恢复版本时返回错误。

CLI 的安装和 `verify` 使用静态文件检查，包括结构、清单、适用的 Python 源码检查和内容摘要。
它们不会导入包代码、调用工厂函数或验证实际运行结果。`requirements.txt` 仍按本机已安装的 Python 依赖校验，不运行 pip。
CLI 安装不请求 securityd 签名审批，包括不执行 `BIANBU_SECURITY_CODESIGN_REQUIRE_DAEMON` 的在线检查；
需要安装期签名策略的流程应使用原有 Runtime/SDK 安装接口，其默认校验行为保留。

### MCP

```bash
bianbu-agent mcp list
bianbu-agent mcp add github --command npx --args -y @modelcontextprotocol/server-github
bianbu-agent mcp add mysrv --command /path/to/server --args --port 1234 --env KEY=VALUE --env OTHER=x
bianbu-agent mcp add ink --url https://mcp.ml.ink/mcp
bianbu-agent mcp add nws --url "https://nws.caseyjhand.com/mcp"
bianbu-agent mcp add mysrv --url https://example.com/mcp --auth header
bianbu-agent mcp login figma --flow browser
bianbu-agent mcp reauth figma --flow device
bianbu-agent mcp get filesystem
bianbu-agent mcp remove filesystem
```

`add --command` 配置本地 stdio MCP，`--args` 和 `--env KEY=VALUE` 只属于 stdio；
`--url` 配置远端 HTTP/SSE MCP，不启动本地进程，不能与 `--env` 同用。
`mcp list` 同时列出系统/全局 MCP 与用户目录中的 MCP。
`--auth header` 和 `--auth oauth` 会交互式读取 token，自动剥离多余的 `Bearer ` 前缀，
并写入 `~/.hermes/.env` 的 `MCP_<NAME>_API_KEY`；MCP 配置只保存环境变量引用。
`login` 强制授权，`reauth --all` 可批量续期；授权流程本身不下载依赖、不启动 MCP 进程。
`get/remove` 接受短名称或完整 ID。独立 MCP 就是一个 `mcps/<id>/mcp.json` 文件，直接用编辑器
落盘即可；面向人的 `mcp show`/`verify` 已删除。

`add` 只做登记：写声明、装包、退出，不下载依赖也不启动 MCP 进程。AgentHub（APM）
调用 agent CLI 有 30s 硬超时，而首次固化 `npx` 依赖（例如 `bilibili-mcp-js` 的 116MB
依赖树）冷缓存下要一分钟以上，安装期做这件事必然被 SIGKILL，留下半装目录和未完成的
安装事务。

- 输出 JSON 除 `id`/`root`/`content_digest` 外还有 `status`、`tools`、`detail`、
  `vendored`、`startup_timeout_seconds`。`add` 一律返回 `status: "preparing"`
  （`tools` 为空），表示声明已登记、服务由 Runtime 启动。
- 真正把声明变成可启动服务的是 Runtime：Worker 每次启动都会为目录里已安装的 MCP
  （不必先被某个专家选中）在后台准备，`npx <包> [参数...]` 声明会在这时用
  `npm install` 落到
  `<data_dir>/mcp-runtime/<id>`（已装好的树直接复用，不再重复 npm），随后启动一次并列出
  工具，成功后重新发布 Generation，工具才出现在专家里。
- 准备中的 MCP 在 Console/API 里是 `preparing`，失败是 `unhealthy` 且 `detail` 给出原因；
  失败只影响这一台 MCP，不再让安装失败，也不再让设备卡在待恢复的安装事务上。
- `--no-verify` 仅为兼容保留（安装不再做启动探测，验证统一由 Runtime 在后台完成）。
- `--upgrade` 覆盖同名 MCP；不加时重复 add 会报 `already exists`。
- `timeout_seconds`（30s）是单次工具调用预算，`startup_timeout_seconds`（120s）是启动与
  握手预算；后台准备不受这两个数字限制。
- `remove` 会一并删除该 MCP 的 `mcp-runtime/<id>` 依赖目录，也会清掉被杀死的安装留下的
  `.install-*`/`.retired-*` 残留目录。

`--args` 是 `REMAINDER`：它之后的参数全部属于 args，`--upgrade`/`--no-verify` 要写在它前面。

`mcp install <目录>` 保留，而且是 AgentHub 的 APM 唯一的 MCP 安装入口：它把下载好的包落到
临时目录，再执行 `bianbu-agent mcp install /tmp/apm-bibit-mcp-XXXX/mcp.nws`，目录名即 MCP
ID。删掉这个动词会让设备上所有来自 AgentHub 的 MCP 安装失败（APM 侧只会看到
`MCP state is unknown`）。

### Model

```bash
bianbu-agent model bootstrap-chat
bianbu-agent model list
bianbu-agent model create local openai-compatible example-model --base-url http://127.0.0.1:8000/v1
bianbu-agent model show local
bianbu-agent model remove local
```

`bootstrap-chat` 在终端交互填写 Provider ID、Base URL 和 API Key，并保存到模型目录。
它不探测模型服务；已配置 Provider 时立即退出，非交互终端返回错误。
`create/show/list/remove` 操作模型 YAML 文件，输出隐藏凭据；本地 Provider 主机白名单校验继续生效。
Agent 使用的具体模型选择仍由 `agent create --provider ... --model ...` 写入专家声明。

### Session

```bash
bianbu-agent session list
bianbu-agent session create session-001 --agent com.bianbu.实际ID --title 写作
bianbu-agent session show session-001
bianbu-agent session remove session-001
```

`create` 必须用 `--agent` 指定所属专家，直接写入 `sessions/<ID>.json`，不会创建 Worker 或发起对话。
`remove` 删除会话记录，不承担停止正在运行的任务或清理全部日志、检查点的职责。

## 响应时间与生效范围

帮助、查询、增删和包管理均无模型或 Worker 启动开销。小型资源的耗时主要来自 Python 启动和本地文件读写。
在 K3（riscv64、Python 3.14.4）上用小型临时资源测量，3 轮独立进程调用的各资源命令中位耗时为 **643–940 ms**，
各级帮助的总体中位耗时约 **302 ms**。测量涵盖表中全部子命令；`bootstrap-chat` 测的是非交互终端立即返回，
不包含用户输入或服务启动时间。这些数据包含每次启动独立 Python 进程的开销。

`bootstrap-chat` 等待用户输入的时间，以及大包复制、摘要校验、大量资源列表和文件锁等待时间，随实际输入和存储状态变化。

文件修改不会主动调用在线重载接口。正在运行的任务可能仍持有原有资源；后续使用由 Runtime 的读取、重载逻辑决定。
`verify` 成功只表示文件检查通过，不表示模型、工具或 MCP 已能成功运行。

## Runtime 服务启动

```bash
# 前台启动控制面，Ctrl+C 停止
bianbu-agent --config /路径/config.yaml serve

# Debian 安装的 systemd 服务（以 bianbu 用户为例）
systemctl status bianbu-agent-runtime@bianbu.service
journalctl -u bianbu-agent-runtime@bianbu.service -f
sudo systemctl restart bianbu-agent-runtime@bianbu.service
sudo systemctl stop bianbu-agent-runtime@bianbu.service
```

`serve`、`serve --role control`、全局 `--role control` 以及内部 `worker` 参数保留。
兼容已有启动脚本，指定 `--config` 而省略子命令仍启动控制面；日常使用建议明确写 `serve`。
`worker` 和 `--role agent-worker` 由服务管理，不用于资源维护。服务启动和模型执行不属于资源命令的快速响应路径。

已删除顶层 `init`、`check`、`build`、`install`、`run`、`stop`、`logs`、`status`、`uninstall`、
`config`、`process`。资源子命令（例如 `skill install`）继续保留。
服务管理使用 systemctl/journalctl；资源管理使用上表命令。
