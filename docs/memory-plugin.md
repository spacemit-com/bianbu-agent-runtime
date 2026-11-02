# Memory 插件

插件贡献一个 MCP Server（`mcps/memory/`，暴露 `memory`、`memory_record`、
`memory_retrieve`、`session_search` 四个工具）、`memory.before_run` /
`memory.after_run` 两个命令 Hook 和 memory-guide。
不再贡献 Console API 或知识库管理页面。记忆保留、隐私、自动提取、偏好应用等策略
仍由 memoryd 管理；本插件继续读取 memoryd 返回的策略，不在本地配置复制这些开关。

## 生命周期与指令记忆

当前插件仅转发可信运行上下文并注入 SDK 的返回值：`before_run` 调用
`MemoryBridge.prepare_context`，`after_run` 调用 `record_runtime_result`。
问候判断、称呼读取、相关性过滤、Instruction 和当前会话 L2 组装均在 Memory SDK；
空闲/重启后的摘要补偿以及显式结束摘要由 memoryd 决定。插件不再扫描其它会话或
直接调用 summarize。即使关闭 daemon 后端使用旧文件模式，读写策略也由 SDK 执行。
这两个 Hook 必须配套安装提供上述方法的新版 SDK 和 memoryd。

- `before_run` 转交运行开始水位，由 SDK 独立读取有效的 Instruction Memory；普通召回
  为空时也能注入指令记忆。每次最多 16 条、每条最多 2000 字符，遵守 memoryd 的
  `memory_enabled` 开关。注入内容作为历史数据，不能覆盖当前用户要求或项目规则。
- `after_run` 不只在成功时调用；失败和已进入运行流程的取消也传递终态，调用
  SDK 的 `record_runtime_result`，由 SDK 分派到 `record_interrupted_turn`，避免把未完成的回答当作正常完成写入。
  在 Worker 尚未开始之前被拒绝或取消的请求，不承诺生成同样的记忆事件。
- 成功的宿主上下文压缩将摘要和压缩标识转交给终态 Hook；Hook 更新会话摘要并写
  检查点（策略由 SDK 实现）。最多携带最近 8 次压缩，摘要传输最多 8000 字符，持久化状态最多
  2000 字符；检测到凭据的摘要不写入。某个摘要写入失败不会跳过正常轮次或取消记录。

压缩持久化发生在本轮终态，而非压缩发生的瞬间；进程被强杀时不保证该 Hook
执行。存储的是摘要与指针，不是完整宿主上下文快照。SDK 的周期性会话摘要与
宿主压缩事件是不同来源。记忆服务故障不能改变聊天本身的终态，但应通过日志和
后端读取核实持久化结果，不能把模型口头确认当作写入成功。

## MCP Server

`mcps/memory/mcp.json` 用 Runtime 展开的解释器变量启动 Runtime 自带解释器：

```json
{
  "command": "${RUNTIME_PYTHON}",
  "args": ["-m", "bianbu_agent_sdk.memory.mcp_server"],
  "env": {"PATH": "${RUNTIME_PYTHON_BIN_DIR}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"}
}
```

`RUNTIME_PYTHON` 由 Runtime 按当前解释器展开（打包实例即
`/usr/lib/bianbu-agent-runtime/venv/bin/python`，源码 checkout 即仓库 `.venv` 的解释器，
也可由同名环境变量覆盖），因此不必把某个安装路径写死。这样做的原因是 memoryd 的身份
校验：它用 `SO_PEERCRED` 比对注册表里的 UID/GID、进程启动时间和 `/proc/<pid>/exe`，
所以 MCP 子进程必须是被 Runtime 直接启动、且可执行文件与 Worker 相同的进程。

SDK 由 `bianbu-agent-memory` 装进 `/usr/lib/python3/dist-packages`，打包 Runtime 的 venv
带 `--system-site-packages`，因此无需额外 `PYTHONPATH`；`mcp` 由 Runtime venv 提供。
源码 checkout 的 venv 没有这个开关，`./go.sh` 会把 SDK 这一个目录写成 venv 级 import
路径（`.pth`）：Worker 起的 Command Hook 与 MCP 子进程环境是被裁剪过的（不继承
`PYTHONPATH`），但都用同一个 venv 解释器，只有 venv 级 import 路径能同时覆盖两者。

Memory 工具的服务端身份不是工具参数：Runtime 在 Worker 的直系子进程环境里注入
`BIANBU_MEMORY_REGISTRATION_SOCKET`（Control 侧 broker，与命令 Hook 同一套），
MCP Server 在进程存活期间一直持有这个连接，broker 在连接存续期间为该注册续期
（24 小时 TTL 每小时前移一次），memoryd 就按注册到的 account/agent 判权；
删不掉的注册会把错误返回给模型，不会静默降级。

每次 `tools/call` 的 `_meta` 里带 `bianbu/host`（`session_id`、`run_id`、
`agent_id`、`account_id`），Memory 工具用它写证据和 run 幂等键，`session_search`
用它排除当前会话。该字段只作作用域提示，不参与判权。

`memory` 的 `remove` 仍是破坏性动作：Runtime 的 HITL 谓词在 securityd 判权之后
仍然要求审批，服务器自己用 `/etc/bianbu-agent-memory/adapter-confirmation.secret`
签一次性确认令牌，记忆删除不需要 Operator 额外配置。

权限能力名随 MCP ID 变为 `mcp.bianbu.memory/memory`（`data` / L2 /
`once_per_session`，与退休前的 `data.memory` 一致）；`mcp.bianbu.memory`
这条旧名字已不再使用。

## 生效配置

`resources/plugins/bianbu.memory/config.yaml` 是插件随包默认配置，实际被加载器读取和校验。
合成顺序为：manifest schema 默认值 → 插件 `config.yaml` → Runtime `plugins`
配置 → `~/.bianbu-agent/plugins.yaml` 的 `config` → 程序化 `PluginSpec.config`。
`bianbu-agent plugin effective` 显示最终值及每个字段的来源。
命令 Hook 从 stdin JSON 的 `config` 读取同一代配置；
运行中的代保持快照，配置变更在重载或下次 Worker 启动后生效。

| 字段 | 默认 | 作用 |
| --- | --- | --- |
| `socket_path` | `null` | 保留 SDK / Worker 环境选择的 memoryd socket；非空路径传给 SDK 客户端 |
| `timeout_seconds` | `2.0` | memoryd 客户端和记忆调用等待预算，范围 `(0, 30]` 秒 |
| `recall_top_k` | `5` | before_run 的召回条数，可填 1–100，但 Memory SDK 会把窗口钳到 **20**，超过 20 的部分不会生效 |
| `recall_max_chars` | `4000` | 同上，召回文字预算，范围 1–100000 |

MCP Server 只从进程环境读三个可选开关：`BIANBU_AGENT_MEMORY_SOCKET`（等价于
`socket_path`）、`BIANBU_MEMORY_TOOL_TIMEOUT`（等价于 `timeout_seconds`）、
`BIANBU_MEMORY_RECALL_TOP_K` / `BIANBU_MEMORY_RECALL_MAX_CHARS`（等价于上面两项）。
要覆盖它们，请在 `mcps/memory/mcp.json` 的 `env` 里声明并重新安装插件。

`session_search` 的完整会话读取仍使用自身有界查询预算。
其它会话只在模型按需调用 `session_search` 时读取，`memory.before_run` 不再注入
其它会话的状态或标题。
显式 socket 必须在当前 Worker 的执行空间可达；配置不会创建 Sandbox 挂载、提升权限，
也不会改变 Runtime 提供的 account/agent 身份。默认 `null` 避免把 Host 路径误用到 Sandbox。

管理员覆盖示例（不要编辑 `/usr/share` 下由 Debian 包管理的默认文件）：

```yaml
schema_version: 1
plugins:
  bianbu.memory:
    enabled: true
    config:
      timeout_seconds: 3.0
      recall_top_k: 3
```

插件整体启用开关使用这里已有的 `enabled`；专家是否使用 Memory 由专家插件选择控制。
`BIANBU_AGENT_MEMORY_ENABLED=false` 只让 `memory.before_run` 停止自动召回，
不再切换到 Runtime 本地记忆：退休的 `memory_record` / `memory_retrieve` 本地
`jsonl` 后端已经删除，MCP 工具始终访问 memoryd，连接失败会明确报告不可用。
