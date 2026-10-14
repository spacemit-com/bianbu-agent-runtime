# Bianbu Agent Runtime

Bianbu Agent Runtime 是一个由 YAML 配置驱动的 Agent 服务，也是 Bianbu AgentOS 的运行时
核心。它负责会话、Agent 生命周期、模型调用、Tool 与插件执行，以及安全边界。

- **会话即事件流**：Session 历史是一份连续 `seq` 的事件日志，刷新、切换会话和多端观看
  得到同一份投影，切换页面不会中断后台 Run。
- **可插拔**：Plugin 可贡献 Tool、Skill、Hook、MCP Server 和 Console 页面。
- **安全边界**：Tool 审批、per-Agent 能力同步、securityd 签名清单，生产模式 fail-closed。
- **模型即配置**：Provider 只保存连接信息，模型名写在每个 Agent 自己的 `agent.yaml` 里。
- **面向端侧**：目前只支持在 SpacemiT K3（riscv64）上运行。

## 文档

| 文档 | 内容 |
| --- | --- |
| [HTTP API 指南](docs/api-guide.md) | `/api/v1` 路由索引、错误码、会话与事件流语义 |
| [CLI 使用指南](docs/cli-guide.md) | `bianbu-agent` 命令行与资源管理 |
| [插件开发指南](docs/plugin-development-guide.md) | Plugin 目录结构、Hook/Tool 调用身份与能力加载 |
| [概要设计](docs/overview-guide.md) · [参考图册](docs/refs/README.md) | 分层、进程模型与关键设计决策 |

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

- 铺开开发默认环境变量（下面每一项都能用同名环境变量覆盖）；
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

## 架构

### 分层与进程模型

- **控制面 Runtime**：FastAPI 服务，持有 Session、Agent 和资源状态，对外提供
  `/api/v1` HTTP 接口与 `WS /api/v1/events/*` 事件流，并管理 Worker 生命周期。
- **Worker**：Run 在 Worker 中执行，模型调用、Tool、Hook 都在 Worker 侧完成。
- **执行模式**：Agent 只声明 `host` 与 `sandbox` 两种执行模式，开发默认 `host`。
- **资源文件化**：配置、插件、模型和会话都是 `data_dir` 下的普通文件，可直接读写和版本化。

### 会话事件与并行状态

会话历史采用「持久事件日志 + 客户端折叠」原则：JSON 与 SQLite RunStore 会把用户消息、
推理、文本增量、Tool、审批、Session inbox 和 Run 终态同步写入每个 Session 独立的连续
`seq` 日志。`GET /api/v1/sessions/{id}/events` 用于分页回放，
`WS /api/v1/events/mux` 在一条连接上推送所有 Session 的已提交事件，
`WS /api/v1/events/host` 推送会话列表与运行状态。客户端按 `seq` 去重，发现缺口后重新拉取
权威历史。

控制台为每个 Session 保留独立的 ViewModel，同一事件窗口同时消费历史和实时事件，并分别
投影成 Chat 与 Trajectory。Chat 面向对话阅读；Trajectory 按 Run 轮次展示用户输入、推理、
助手输出、Tool、审批、上下文压缩和终态，可搜索记录、查看原始输入/输出、耗时与 Token 汇总。
两个视图共用历史向前分页和实时 mux，不维护第二份轨迹存储。会话列表返回并展示
`running`、`waiting`、`failed`、`completed_unread`、`active_run_id` 和 `pending_interaction`，
因此切换页面不会停止后台任务，未选中会话完成后也会显示提醒。浏览器不连接单个 Run，也不在
切换会话时重连，所以刷新和切换得到相同投影。

## 安全

### Tool 审批

Tool 元数据声明 `requires_approval=true` 时，该 Tool 每次执行前都会等待用户批准。除此之外，
Runtime 会动态检查 `terminal` 的命令参数：只要检测到独立的 `rm` 命令词（包括脚本、heredoc、
包装器和子 Shell 中的 `rm`）就强制触发 approval，批准前不会执行终端 Tool。

### Agent 安全生命周期

Runtime 会把逻辑 Agent（默认 Agent、API 创建的专家以及导入的 `.agent` 包）在持久化后注册
到 securityd，并在 Worker 启动前用 Agent 声明的 `permissions.capabilities`、tools、skills、
MCP 和 hooks 做一次替换式能力同步。这条逻辑身份注册不要求每个 Agent 都具备 signed Runtime
manifest；旧的受控 Worker lease 仍可由生产部署单独启用。

`./run.sh` 面向开发态，默认 `BIANBU_SECURITY=1`、`BIANBU_RUNTIME_SECURITY=required`、
`BIANBU_SANDBOX=off`，并且不给 Worker 施加 cgroup `MemoryMax`/`CPUQuota`：单板设备上没有
需要保护的其它租户，长会话却可能被固定限额杀掉。需要复现限额时显式给一个值即可，例如
`BIANBU_WORKER_MEMORY_MAX=2G ./run.sh`。要测试旧的受控 sandbox lease，显式设置
`BIANBU_SANDBOX=agent`。

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

## 模型与 Agent 配置

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
```

运行时严格按 Agent 保存的 `provider + name` 解析，不存在全局默认服务商或默认模型。
`provider.yaml` 使用 `0600` 权限，因为其中可能包含 API Key。

Agent 配置里 `name` 是调用名，`name_zh`/`name_en` 是 Console 与 Bibit 各自语言下的展示名，
`description`/`description_zh` 是对应的界面简介；展示字段为空时回退到 `name`。
`description` 不进入 LLM 上下文，模型行为由 `soul` 定义。新的 `agent.yaml` 写成
`schema_version: 2`（与 AgentHub 的 Power 格式 v2 一致），版本 1 的记录仍可读取。Agent HTTP
API 同样使用这些字段和 `soul`，不再接受旧的 `instructions` 字段。

## 数据目录

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

## 本地资源管理

`bianbu-agent` 直接读写资源文件，不启动 Worker 或加载执行框架：

```bash
bianbu-agent --help
bianbu-agent agent create Writer --provider my-provider --model my-model
bianbu-agent agent list
bianbu-agent skill install /路径/example.skill
bianbu-agent plugin effective
bianbu-agent session create session-001 --agent com.bianbu.实际ID
```

内置工具源码按职责组织在 `bianbu_agent/tools/`，用户工具目录仅用于扩展。

## 安装 MCP Server

最简单的方式是在 Web 控制台进入「MCP」页面，填写名称和一行启动命令后点击「安装」。
例如安装 Filesystem MCP，并只开放 `/data/projects`：

```text
npx -y @modelcontextprotocol/server-filesystem /data/projects
```

安装成功后，MCP 页面显示连接状态、启动命令和发现的内部工具。MCP 不会出现在「工具」页面；
请在专家编辑器的 MCP 区域按整台 Server 授权。新建和已有专家默认都不使用任何 MCP，
`tools: ["*"]` 也不会授予 MCP 权限。页面会按 POSIX 参数规则解析命令并直接启动程序，
不经过 Shell，因此不支持管道、重定向、命令替换等 Shell 语法。只应安装可信来源，并为
Filesystem MCP 指定最小必要目录。

需要 HTTP transport、环境变量或请求头时，可以使用高级目录安装方式。一个目录对应一个
Server，目录名是 MCP ID，目录内只放置 `mcp.json`：

```text
acme.filesystem/
└── mcp.json
```

```json
{
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data/projects"],
  "env": {"LOG_LEVEL": "info"}
}
```

CLI 也可完成同样的操作（`bianbu-agent ... mcp add/get/list/remove`）。安装、移除 MCP 后
需要重启正在运行的 Runtime，新服务才会进入活动 Generation；源码启动的实例按 Ctrl-C 停止后
重新执行 `./run.sh` 即可：

```bash
bianbu-agent --config ~/.bianbu-agent/config.yaml \
  mcp add filesystem --command npx \
  --args -y @modelcontextprotocol/server-filesystem /data/projects
bianbu-agent --config ~/.bianbu-agent/config.yaml mcp list
bianbu-agent --config ~/.bianbu-agent/config.yaml mcp get filesystem
bianbu-agent --config ~/.bianbu-agent/config.yaml mcp remove filesystem
```

## K3 与 Bibit Intent

K3 上的端侧意图与模型能力由独立的 `bibit-intent` Debian 包提供，
`bianbu-agent-runtime` 已声明对它的运行时依赖；刷新索引并安装或更新 Runtime 时，APT 会
自动安装匹配的 Bibit 包。

端侧模型和 service 生命周期都归 bibit-intent 包自己：它安装并全局启用
`bibit-intent.service`，再由该单元 `Wants` 的 `bibit-intent-models.service` 在 AI Gateway
里准备 `bibit-llm`/`bibit-embed`。Runtime（包括 `./run.sh`）不代为 enable/start，只探测
一次它对外的 `http://127.0.0.1:8765/healthz` 报告就绪状态；未就绪不会阻塞 Runtime 启动
（端侧不可用时对话按云端降级）。这些全局启用的 user unit 随图形桌面会话启动，只重新连接
SSH 不会重建已经存在的桌面 user manager。验证命令：

```bash
systemctl --user is-active bibit-intent bibit-intent-models
curl -fsS http://127.0.0.1:8765/healthz
curl -fsS http://127.0.0.1:9899/api/v1/runtime/status
```

服务没起来时按 bibit-intent 自己的说明排查：`systemctl --user status bibit-intent.service`。
生产 Worker 通过 `127.0.0.1:8765` 的稳定 HTTP 协议调用 Bibit，不需要把 `/opt/bibit-intent`
挂进 Runtime 沙箱；签名网络策略显式声明 `localhost`，只为 Worker 放行 Bibit 的回环服务，
不开放局域网或任意外网地址。

## Debian 安装后的系统 Runtime 服务

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

## 验证

```bash
uv run pytest -q
uv build
```
