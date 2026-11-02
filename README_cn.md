# Bianbu Agent Runtime

[English](README.md) | 简体中文

Bianbu Agent Runtime 是一个由 YAML 配置驱动的 Agent 服务，也是 Bianbu AgentOS 的运行时
核心。它负责会话、Agent 生命周期、模型调用、Tool 与插件执行，以及安全边界。

- **会话即事件流**：Session 历史是一份连续 `seq` 的事件日志，刷新、切换会话和多端观看
  得到同一份投影，切换页面不会中断后台 Run。
- **可插拔**：Plugin 可贡献 Tool、Skill、Hook、MCP Server 和 Console 页面。
- **安全边界**：Tool 审批、per-Agent 能力同步、securityd 信任链，生产模式 fail-closed。
- **模型即配置**：Provider 只保存连接信息，模型名写在每个 Agent 自己的 `agent.yaml` 里。
- **面向端侧**：目前只支持在 SpacemiT K3（riscv64）上运行。

## 文档

| 文档 | 内容 |
| --- | --- |
| [HTTP API 指南](docs/api-guide.md) | `/api/v1` 路由索引、错误码、会话与事件流语义 |
| [CLI 使用指南](docs/cli-guide.md) | `bianbu-agent` 命令行与资源管理 |
| [插件开发指南](docs/plugin-development-guide.md) | Plugin 目录结构、Hook/Tool 调用身份与能力加载 |
| [概要设计](docs/overview-guide.md) · [参考图册](docs/refs/README.md) | 分层、进程模型与关键设计决策 |
| [安全插件](docs/security-plugin.md) | `bianbu.security` 如何把 Runtime Hook 映射到 securityd |
| [DeepSeek Harness 笔记](docs/deepseek-harness-notes.md) | 对齐 Harness 的行为与源码证据 |

## 架构

### 进程模型

```
bianbu-agent-runtime.service            （整机一个，开机启动）
└─ 控制面（FastAPI，/api/v1，WS /api/v1/events/*）
   ├─ Session、Run、事件、文件与 SQLite 存储
   ├─ Agent 与资源清单
   ├─ WorkerSupervisor ──► 每个 Agent 一个 Worker
   │                         ├─ 模型、Tool、MCP、Skill
   │                         ├─ PluginGeneration（含 bianbu.security）
   │                         └─ 安全中间件（DLP / 审批 / 拒绝）
   └─ SecurityRuntimeClient ──► securityd（prepare/commit/abort start、lease）
```

- **控制面 Runtime**：FastAPI 服务，持有 Session、Agent 和资源状态，对外提供
  `/api/v1` HTTP 接口与 `WS /api/v1/events/*` 事件流，并管理 Worker 生命周期。
- **Worker**：Run 在 Worker 中执行，模型调用、Tool、Hook 和 MCP 都在 Worker 侧完成，
  不会阻塞控制面。
- **执行模式**：Agent 声明 `host` 或 `sandbox`，由 securityd 决定 effective mode 并签发
  启动 lease。
- **资源文件化**：配置、插件、模型和会话都是 `data_dir` 下的普通文件，可直接读写和版本化。

### 源码结构

| 路径 | 职责 |
| --- | --- |
| `bianbu_agent/api/` | `/api/v1` 路由：Agent、Session、Run、安全、包管理与监控 |
| `bianbu_agent/server/` | 组合根、Runtime 启动与 Worker 监督 |
| `bianbu_agent/sessions/` | Session 日志、投影、SQLite 与 JSON 存储 |
| `bianbu_agent/runs/` | Run 管理、Worker RPC 与 Checkpoint |
| `bianbu_agent/engines/` | `AgentEngine` 契约与默认 LangGraph 实现 |
| `bianbu_agent/tools/` | 内置工具（文件、进程、Skill、Web、浏览器、视觉、看板） |
| `bianbu_agent/plugins/` | Plugin 宿主与贡献加载 |
| `bianbu_agent/hooks/` | Hook 协议、命令 Hook 与失败处理 |
| `bianbu_agent/mcp/` | MCP Server 安装、健康检查与凭据 |
| `bianbu_agent/security/` | securityd 客户端、中间件、沙箱、DLP 与系统调用策略 |
| `bianbu_agent/console/web/` | 内置 Web 控制台静态资源 |
| `bianbu_agent/cli/` | `bianbu-agent` 命令行 |
| `resources/` | 包内置的 Starter Agent、系统插件、模型与 MCP 定义 |
| `debian/` | Debian 打包、systemd unit、polkit 与 provisioning 脚本 |

### 会话、Run 与事件

会话历史采用「持久事件日志 + 客户端折叠」原则：JSON 与 SQLite RunStore 会把用户消息、
推理、文本增量、Tool、审批、Session inbox 和 Run 终态同步写入每个 Session 独立的连续
`seq` 日志。`GET /api/v1/sessions/{id}/events` 用于分页回放，
`WS /api/v1/events/mux` 在一条连接上推送所有 Session 的已提交事件，
`WS /api/v1/events/host` 推送会话列表与运行状态。客户端按 `seq` 去重，发现缺口后重新拉取
权威历史。

控制台为每个 Session 保留独立的 ViewModel，同一事件窗口同时消费历史和实时事件，并分别
投影成 Chat 与 Trajectory。Chat 面向对话阅读；Trajectory 按 Run 轮次展示用户输入、推理、
助手输出、Tool、审批、上下文压缩和终态，可搜索记录、查看原始输入/输出、耗时与 Token 汇总。
两个视图共用历史向前分页和实时 mux，不维护第二份轨迹存储。

## 快速开始

> **运行平台**：当前版本只支持在 SpacemiT K3（riscv64，Bianbu 4.0）上运行，其余平台尚未验证；
> 下面的命令和 `scripts/dev-provision.sh` 都假定目标设备就是 K3。

### 1. 准备环境（一次性）

```bash
git clone <仓库地址> bianbu-agent-runtime
cd bianbu-agent-runtime
./scripts/dev-provision.sh
```

`dev-provision.sh` 是幂等的，只准备环境、不启动 Runtime：

- 通过 APT 安装/升级运行时依赖（与 `debian/control` 的 `Depends` 对齐）；
- 安装 `uv`（缺失时装到 `~/.local/bin`）；
- 把当前用户加入 `bianbu-agent`、`bianbu-security` 组；
- 拉起 `securityd`、`memoryd` 并等待 socket 就绪；
- 准备 SearXNG 与本地模型记录的权限。

脚本需要 `sudo`；非交互场景（CI、远程 ssh）可通过 `SUDO_ASKPASS` 传入密码。加组要重新
登录（或重开终端）后生效。

### 2. 启动

```bash
./run.sh
```

`run.sh` 只做两件事——准备开发配置，然后 `exec` 源码 Runtime：

- 铺开开发默认环境变量（每一项都能用同名环境变量覆盖）；
- 把 `config/development.yaml` 与开发自签安全清单合并成
  `.bianbu-agent/config-all.yaml`；
- 生成 Starter Agent，并注册开发自签安全身份（这一步需要 `sudo`）；
- 停掉占用同一端口的已安装系统服务，然后在源码目录里拉起 Runtime。

浏览器打开 <http://localhost:9899/chat> 进入控制台。

### 3. 手动启动（不使用脚本）

```bash
uv sync --locked
export BIANBU_SYSTEM_RESOURCE_ROOT="$PWD/resources"
uv run bianbu-agent --config config/development.yaml model bootstrap-chat
uv run --locked bianbu-agent --config config/development.yaml
```

`model bootstrap-chat` 会交互式询问 Provider 连接信息和模型名并写入本地数据目录；已配置
时直接退出。Runtime 会生成 `com.bianbu.<12 位随机十六进制>` 形式的 Agent ID，并在命令
输出里返回；任何 Agent 都可以删除。

项目源码和 Linux riscv64 上的 SpacemiT wheel 统一使用 Python 3.14，仓库根的
`.python-version` 让 `uv` 自动选择，依赖由提交的 `uv.lock` 严格锁定。

## 配置

后端全局只保存服务商连接信息和凭据，具体模型名不建立全局记录，而是直接写入每个 Agent 的
`agent.yaml`：

```text
models/
├── openai/
│   └── provider.yaml
└── anthropic/
    └── provider.yaml

agents/writer/agent.yaml:
  model:
    provider: openai
    name: gpt-5
    effort: low
```

运行时严格按 Agent 保存的 `provider + name` 解析，不存在全局默认服务商或默认模型；
`effort` 随该模型选择一起保存，取值为 `off`、`low`、`high`、`max`，省略时为 `low`。
`provider.yaml` 使用 `0600` 权限，因为其中可能包含 API Key。

Agent 配置里 `name` 是调用名，`name_zh`/`name_en` 是 Console 与 Bibit 各自语言下的展示名，
`description`/`description_zh` 是对应的界面简介；展示字段为空时回退到 `name`。
`description` 不进入 LLM 上下文，模型行为由 `soul` 定义。新的 `agent.yaml` 写成
`schema_version: 2`（与 AgentHub 的 Power 格式 v2 一致），版本 1 的记录仍可读取。Agent HTTP
API 同样使用这些字段和 `soul`，不再接受旧的 `instructions` 字段。

### 数据目录

`data_dir` 下可包含 `agents/`、`models/`、`sessions/`、`skills/`、`mcps/`、`plugins/`、
`workspace/` 和 `logs/`；`skills/`、`mcps/`、`plugins/` 等扩展根由写入它们的 manager 按需
创建。独立 MCP 位于 `mcps/<mcp-id>/`。默认插件位于 `plugins/<plugin-id>/`，可贡献 Hook、
Tool、Skill、Console，以及 `mcps/<name>/` 下的多个 MCP Server。

服务与资源管理命令使用显式 `--config`；项目开发命令会依次查找项目内 `runtime.yaml` 和
`BIANBU_AGENT_CONFIG`：

```yaml
data_dir: .

service:
  title: Bianbu Agent
  host: 0.0.0.0
  port: 9899
```

相对 `data_dir` 以配置文件所在目录为基准，`~` 会展开，因此配置可以放在
`~/.bianbu-agent/config.yaml`、项目目录或任意其它位置。Runtime 没有全局默认 Agent，
每个 Session 都显式记录使用的 Agent ID。HTTP 调用仅使用 `/api/v1`。

## 安全边界

### Tool 审批

Tool 元数据声明 `requires_approval=true` 时，该 Tool 每次执行前都会等待用户批准。除此之外，
Runtime 会动态检查 `terminal` 的命令参数：只要检测到独立的 `rm` 命令词（包括脚本、heredoc、
包装器和子 Shell 中的 `rm`）就强制触发 approval，批准前不会执行终端 Tool。

### Agent 安全生命周期

Runtime 会把逻辑 Agent（默认 Agent、API 创建的专家以及导入的 `.agent` 包）在持久化后注册
到 securityd，并在 Worker 启动前用 Agent 声明的 `permissions.capabilities`、tools、skills、
MCP 和 hooks 做一次替换式能力同步。这条逻辑身份注册不要求每个 Agent 都具备单独的签名清单；
受控 Worker lease 仍可由生产部署单独启用。删除专家时，Runtime 先调用 securityd 的
`UnregisterAgent`，确认成功后才删除本地声明；注销失败会保留专家并返回错误。

`./run.sh` 面向开发态，默认 `BIANBU_SECURITY=1`、`BIANBU_RUNTIME_SECURITY=required`、
`BIANBU_SANDBOX=off`，并且不给 Worker 施加 cgroup `MemoryMax`/`CPUQuota`/`TasksMax`：
单板设备上没有需要保护的其它租户，长会话却可能被固定限额杀掉或撑爆 task 上限。需要复现
限额时显式给一个值即可，例如 `BIANBU_WORKER_MEMORY_MAX=2G ./run.sh`。要测试受控
sandbox lease，显式设置 `BIANBU_SANDBOX=agent`。

生产环境应由受信 Runtime Manager 启动，并把 `BIANBU_RUNTIME_MODE=production` 与
`BIANBU_RUNTIME_SECURITY=required` 配置为 fail-closed；`./run.sh` 会拒绝裸启动生产模式。

管理员可在 Agent 的 Security/能力页开启 per-Agent YOLO 模式：所有当前、未知和未来的
capability/scope 会免确认放行，但 caller validation、sandbox、网络策略、DLP、审计和
securityd fail-closed 保持启用。模式由 securityd 权威保存，关闭后恢复原粒度授权；不要用
环境变量或 Agent 配置模拟 YOLO。

### 部署提示

插件是宿主进程内的可信 Python 代码，不是操作系统沙箱。真实凭据和运行数据不应提交到版本
控制。当前版本在开发模式和 Debian 包生产模式中都暂时禁用了 Web/管理 API 的管理员 Token
门禁，Web Console 不需要手动输入 Token；服务只应部署在受信任网络，暴露到公网前必须在反向
代理或网关增加认证、TLS 和访问控制。stdio MCP Server 同样会以服务用户身份启动子进程，
因此只应安装可信配置和程序。

## 对接

### securityd

Runtime 通过 D-Bus `io.bianbu.Security` 与 securityd UDS 通信。控制面在 Worker 启动时
执行 prepare / commit / abort，取得 effective execution mode 和 lease，并把 lease 身份
传给 Worker。Worker 运行 `bianbu.security` 插件，把 Runtime Hook 映射到 securityd 干预点：

| Runtime Hook | securityd 干预点 |
| --- | --- |
| `BeforeRun` | `agent_startup` |
| `AfterContext` | `input` |
| `BeforeModel` | `pre_model_call` |
| `AfterModel` | `post_model_call` |
| `PreToolUse` | `pre_tool_call` |
| `PostToolUse` | `post_tool_call` |
| `AfterRun` | `output` / `agent_shutdown` |

完整的映射、DLP 行为和失败语义见[安全插件](docs/security-plugin.md)。

### hermes-agent

Runtime 会把 hermes Agent 注册到 securityd，并根据 `framework` 字段或 `com.hermes*`
形式的 Agent ID 推断框架，因此控制台会为它显示 hermes 工具目录。Runtime 的对账逻辑
永远不会删除 `com.hermes.*` 记录：securityd 是 runtime、hermes 和 DeepSeek Harness
共用的注册表。

hermes 侧适配器位于 `bianbu-agent-security` 仓库的
`impl/bianbu-securityd/hermes_plugin/`。它是一个 hermes 插件，在工具执行前调用 securityd，
绑定由 profile 派生的 Agent ID（`com.hermes.<profile>`），并对工具参数、工具结果和最终输出
做 DLP。用 `hermes_plugin/scripts/install-on-k3.sh hermes` 安装。Runtime 本身不运行
hermes，也不 import hermes 代码。

### DeepSeek Harness（dsh）

DeepSeek Harness 是独立运行时。Runtime 的安全注册已经接受 `harness` 框架，securityd
也有独立的 `harness` 工具目录，所以 dsh Agent（`com.dsh.agent`）会和其它 Agent 一起出现在
同一个安全控制台里。

dsh 适配器位于 `bianbu-agent-security` 仓的
`impl/bianbu-securityd/harness_plugin/`：一个 out-of-tree Node bundle 加一个 Python
sidecar，sidecar 通过 stdio 上的 JSON-RPC 调用 securityd。`tools/pre-execute` 映射到
`pre_tool_call`，`tools/post-execute` 映射到 `post_tool_call`，`agent/pre-step` 映射
到 `input`。dsh 没有可以改写已提交 assistant 消息或 provider 请求的公开钩子，因此适配器
不会声称覆盖输出侧脱敏。安装时在该安全仓执行
`impl/bianbu-securityd/harness_plugin/scripts/install-on-k3.sh dsh`。

DeepSeek Harness 同时也是本仓的行为参考。带上游 revision 与文件级证据的设计笔记见
[DeepSeek Harness 笔记](docs/deepseek-harness-notes.md)。

## 模型、工具与 MCP

每个 Agent 在 `agent.yaml` 中保存模型，运行时按此解析。默认引擎是 LangGraph，但
`AgentEngine` 是抽象契约，`engine_builder` 可注入其它实现；插件契约里不出现 LangGraph。

内置工具源码按职责组织在 `bianbu_agent/tools/`，用户工具目录仅用于扩展。MCP Server 可
从控制台 MCP 页面或 CLI 安装。stdio MCP 命令按 POSIX 参数规则解析并直接执行，不经过
Shell，因此不支持管道、重定向、命令替换等语法。只应安装可信来源，并授予最小必要目录。

```bash
bianbu-agent --config ~/.bianbu-agent/config.yaml \
  mcp add filesystem --command npx \
  --args -y @modelcontextprotocol/server-filesystem /data/projects
bianbu-agent --config ~/.bianbu-agent/config.yaml mcp list
bianbu-agent --config ~/.bianbu-agent/config.yaml mcp remove filesystem
```

安装、移除 MCP 后需要重启正在运行的 Runtime，新服务才会进入活动 Generation。

## K3 与 Bibit Intent

K3 上的端侧意图与模型能力由独立的 `bibit-intent` Debian 包提供，
`bianbu-agent-runtime` 已声明对它的运行时依赖；刷新索引并安装或更新 Runtime 时，APT 会
自动安装匹配的 Bibit 包。

端侧模型和 service 生命周期都归 bibit-intent 包自己：它安装并全局启用
`bibit-intent.service`，再由该单元 `Wants` 的 `bibit-intent-models.service` 在 AI Gateway
里准备 `bibit-llm`/`bibit-embed`。Runtime（包括 `./run.sh`）不代为 enable/start，只探测
一次它对外的 `http://127.0.0.1:8765/healthz` 报告就绪状态；未就绪不会阻塞 Runtime 启动
（端侧不可用时对话按云端降级）。

```bash
systemctl --user is-active bibit-intent bibit-intent-models
curl -fsS http://127.0.0.1:8765/healthz
curl -fsS http://127.0.0.1:9899/api/v1/runtime/status
```

生产 Worker 通过 `127.0.0.1:8765` 的稳定 HTTP 协议调用 Bibit，不需要把 `/opt/bibit-intent`
挂进 Runtime 沙箱；签名网络策略显式声明 `localhost`，只为 Worker 放行 Bibit 的回环服务，
不开放局域网或任意外网地址。

## Debian 安装

DEB 安装一个整机唯一的系统级服务 `bianbu-agent-runtime.service`，由 `multi-user.target`
拉起：开机即启动，跟谁登录无关。运行身份是安装时解析出的设备用户，记录在
`/etc/bianbu-agent-runtime/service-user`，并通过 drop-in
`/etc/systemd/system/bianbu-agent-runtime.service.d/10-service-user.conf` 覆盖包内默认值
（`User=` 读不了配置文件，只能这样下发）。这么设计是为了让 host Worker 保持真实登录身份：
家目录、文件资源、sudo/PAM 都依赖它。安装时找不到设备用户（镜像 chroot、容器、CI）就保留
包内默认的 `bianbu-runtime` 账号，并且不 enable。

持久状态位于服务用户的 `~/.bianbu-agent/`，控制台端口由 UID 推出：UID 1000 是 9899，
其余是 `9899 + ((uid - 1000) % 55637)`。Host 与 bwrap Worker 继续使用 system transient
scope，polkit 只放行来自 `bianbu-agent-runtime.service` 的调用，不给本地会话任何 unit 管理
权限——控制台在浏览器里按端口打开，不需要有人从桌面把它拉起来。

系统级 DEB 只需安装一次，服务开机自启；以下命令用于排查：

```bash
sudo systemctl status bianbu-agent-runtime.service
journalctl -u bianbu-agent-runtime.service
sudo systemctl restart bianbu-agent-runtime.service
```

整机只有一个数据目录，所以调用者不需要猜它在谁家里，按顺序解析即可：

1. `BIANBU_AGENT_CONFIG`（显式指定，优先级最高）；
2. `/etc/bianbu-agent-runtime/service-user` 里记录的账号，取它的
   `~/.bianbu-agent/config.yaml`——命令行工具（`bianbu-agent`）和 Worker 都走这一步，
   所以 `sudo bianbu-agent model list` 不会落到 `/root/.bianbu-agent`；
3. 没有安装记录（源码目录、开发机）时回落到当前用户的 `~/.bianbu-agent/config.yaml`。

`/api/v1/runtime/status` 也返回 `service_user` 和 `data_dir`，控制台的「Runtime 状态」页
会显示。开发联调仍可用 `BIANBU_AGENT_CONFIG=/path/config.yaml` 显式覆盖配置。

预装 Agent、Skill、Plugin、MCP 和模型定义由系统包提供，统一安装到
`/usr/share/bianbu-agent-runtime/`，升级可直接替换。内置工具位于 Python 包的
`bianbu_agent/tools/`；默认 Agent 的稳定实例位于 `/var/lib/bianbu-agent-profiles/`。
AgentHub 安装、应用编辑产生的资源和运行数据写入 `~/.bianbu-agent/`，同 ID 的用户定义优先。
部署语言 `deployment.language` 仅支持 `zh`（简体中文）和 `en`（英文）。

## 源码构建

```bash
uv sync --locked                 # 创建锁定的 Python 环境
uv build                         # 构建 wheel/sdist
dpkg-buildpackage -us -uc -b     # 构建 Debian 包
```

Debian 构建会在 `/usr/lib/bianbu-agent-runtime` 下生成可重定位的锁定虚拟环境，
并执行 `debian/tests/` 中的打包测试。在 riscv64 上还会下载
固定版本的 SpacemiT `sqlite-vec` wheel 并校验 SHA-256。

生成的 `bianbu-agent-runtime_<版本>_<架构>.deb` 用
`sudo apt install ./bianbu-agent-runtime_*.deb` 安装。

## 验证

```bash
uv run pytest -q
uv build
```

开发记录、发布说明与真机验收结果见 `debian/changelog` 与上表文档。
