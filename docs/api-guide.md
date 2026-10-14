# Bianbu Agent HTTP API 指南

本指南是 Bianbu Agent 已发布 HTTP 合约的唯一人工可读文档，覆盖接口边界、请求与响应、
前端接入流程、运维注意事项和完整路由索引。运行中的机器可从 `/openapi.json` 取得同一份
机器可读定义，并可在 `/docs` 或 `/redoc` 浏览它。浏览器控制台位于 `/chat`。

## API 边界

所有 API 资源都位于唯一的版本化命名空间 `/api/v1`。Run、Session、Agent、Provider、
扩展、设置和监控资源使用同一套资源语义，不再区分 public/admin 路由。

当前版本暂时禁用内置管理员 Bearer Token 认证，包括 Debian 包启动的生产模式。管理类资源
不会要求令牌，`GET`/`POST /api/v1/auth/session` 固定返回
`{"data":{"authenticated":true,"required":false}}`，Web Console 不再弹出 Token 输入框。
认证路由暂时保留，以免前端需要条件分支；它们不建立登录 Cookie，也不构成安全边界。
应用不提供 RBAC 或 CORS，部署方必须通过受信任网络或反向代理实施 TLS、来源限制和访问控制。

当前 Run 准入、Session 删除和待处理动作锁是进程内状态，因此服务只支持**单进程、单 worker**
部署。内置 CLI 和 `serve_app` 都显式以一个 Uvicorn worker 启动；不要在外部启动器中复制应用
进程。需要扩展容量时，应在保持单实例写入边界的前提下先引入跨进程协调存储。

这是一次**破坏性变更**：服务只发布上述版本化命名空间，**不提供兼容**路由、别名或
请求转换层。客户端必须按本指南或运行时 OpenAPI 调用资源；不要保留旧的扁平端点示例。

## 通用约定

JSON 成功响应使用规范 envelope：

```json
{"data": {}}
```

集合响应将 `data` 设为数组，并附带分页信息；例如：

```json
{"data": [], "page": {"number": 1, "size": 50, "has_more": false}}
```

错误始终使用同一 envelope：

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request validation failed.",
    "details": {"errors": []}
  }
}
```

客户端应以 HTTP 状态和稳定的 `error.code` 分支，而不是解析错误消息。下表列出当前路由
和统一异常处理器发出的稳定代码；其中 `details` 提供验证错误、资源 ID 或冲突上下文。

| `error.code` | HTTP 状态 | 客户端分支条件 |
| --- | --- | --- |
| `bad_request` | `400` | 请求 JSON 或 Content-Length 格式无效 |
| `forbidden` | `403` | 请求违反受保护资源边界 |
| `provider_host_rejected` | `403` | Provider `base_url` 不在服务端允许的主机列表中 |
| `not_found` | `404` | 请求的资源、Run、包或 Plugin 不存在 |
| `conflict` | `409` | 通用资源写入冲突，例如已存在的包或 Plugin |
| `run_conflict` | `409` | Run 已终态、不是活动 Run，或消息删除不能被接受 |
| `idempotency_conflict` | `409` | 幂等键已绑定到不同的 Run 请求 |
| `session_agent_mismatch` | `409` | 已绑定 Session 收到不同的 `agent_id` |
| `session_input_conflict` | `409` | 同一 `message_id` 被重放为不同的 Session 输入 |
| `model_integrity_conflict` | `409` | Provider 内部记录的完整性约束冲突 |
| `provider_in_use` | `409` | Provider 仍被一个或多个 Agent 引用 |
| `mcp_already_installed` | `409` | 同 ID 的托管 MCP Server 已安装 |
| `mcp_owned_by_plugin` | `409` | MCP Server 由 Plugin 提供，必须删除所属 Plugin |
| `mcp_activation_failed` | `409` | 安装后激活 MCP Server 失败，安装已尝试回滚 |
| `mcp_reload_failed` | `409` | 删除后重载 MCP Runtime 失败，删除已尝试回滚 |
| `plugin_activation_failed` | `409` | 安装后激活 Plugin 失败，安装已尝试回滚 |
| `plugin_reload_failed` | `409` | 删除后重载 Plugin Runtime 失败，删除已尝试回滚 |
| `plugin_managed_by_system` | `409` | Plugin 由系统包管理（随 Runtime 包安装），不能在控制台卸载 |
| `model_rollback_failed` | `500` | 模型多记录事务回滚失败 |
| `internal_error` | `500` | 未预期的 API 持久化或 Runtime 故障；消息经过脱敏且不含内部异常文本 |
| `payload_too_large` | `413` | Agent 头像超过 2 MiB |
| `unsupported_media_type` | `415` | 头像不是支持的图片 MIME 类型或内容不匹配 |
| `validation_error` | `422` | 严格请求字段、查询参数、路径参数或值域验证失败 |
| `http_error` | `405` | 未由专用代码映射的框架 HTTP 错误；`405 Method Not Allowed` 是代表情形 |
| `bad_gateway` | `502` | 上游依赖连接失败（含本地模型状态读取失败） |

请求模型会拒绝未知字段。

所有 JSON 写入请求使用 `Content-Type: application/json`；Agent 头像上传使用
`multipart/form-data`；运行日志使用 `text/event-stream`，对话实时事件使用 WebSocket。

## Session 执行与控制

Runtime 不公开 Run 资源。Run 只是服务端内部执行单元；客户端通过 Session 提交输入、恢复
状态、停止执行和处理审批。首次输入和运行中的调整使用同一个 prompt 资源，空闲 Session
由服务端内部启动执行。

停止当前执行：

```bash
curl -sS -X POST "$BASE_URL/api/v1/sessions/demo-session/cancel"
```

审批动作提交 `{"decision":"approve"|"reject","reason":"..."}`。客户端必须处理 Session
日志中的文本、推理、工具、inbox、审批、错误与终态事件，不能假定事件流只包含文本。
模型返回未绑定的工具名时，运行时输出 `tool_rejected`，不会输出该调用的
`tool_start`/`tool_end`；客户端应将它显示为被权限边界拒绝的模型尝试，而不是正在执行的工具。

### 一次完整聊天流程

1. `GET /api/v1/agents` 选择 `available: true` 的 Agent。
2. `POST /api/v1/sessions` 创建并绑定 Session；需要切换 Agent 时创建新 Session。
3. `POST /api/v1/sessions/{id}/prompt` 提交用户输入；空闲 Session 会自动创建 Run。
4. 首次打开 Session 时分页读取 `/sessions/{id}/history` 显示窗口；发现 `seq` 缺口时
   回读 `/sessions/{id}/events` 原始窗口；实时更新来自共享的 `/events/mux` 与
   `/events/host` WebSocket。已经持有连续游标的客户端用 `/events?from_seq=N` 增量补洞，
   不再从窗口尾部反复向前翻页。
5. 遇到 `approval_required` 时调用 Session 审批资源；运行中改方向仍向 Session prompt 提交输入；停止调用 Session cancel。
6. 页面恢复时读取 Session 原始事件日志；同一个事件窗口驱动历史和实时投影。

Web Console 的 Chat 与 Trajectory 读取同一个 Session 显示窗口。Trajectory 是纯客户端
投影：它按 Run 组织用户、推理、助手、Tool、审批、压缩和终态记录，并计算已加载窗口内的
耗时及 Token 汇总。切换视图不会创建新请求或复制持久数据；向前加载仍使用同一个
`before_seq` 历史接口，实时追加仍来自 `/events/mux`。

标准聊天提交以 `message_id` 幂等：网络失败重试复用原 ID，用户主动再次发送时生成新 ID。
服务端不公开 Run 创建或快照资源；`run_id` 仅作为 Session 事件中的内部执行关联标识。

## Session 与 Agent

Session 保存 Agent 绑定、标题、元数据和对话线程。创建 Session 时可提供 `agent_id`、`title`
和 `metadata`；更新只允许 `title`。清空上下文会保留 Session 而切换到新的对话线程。删除
Session 会删除其内部执行记录，并尽力
清理 checkpoint。

创建 Agent 时必须提供 `name` 和结构化的
`model: {"provider":"...","name":"..."}`，还可提供 `name_zh`、`name_en`、`description`、
`description_zh`、`soul`、`tools`、`skills`、`mcps`、`plugins` 和 `metadata`。Runtime 为每个新 Agent 生成
`com.bianbu.<12位随机十六进制>` ID；请求携带 `id` 会返回 `422 validation_error`。Agent 不再声明
Hook：Hook 属于 Plugin，选择 Plugin 即激活它提供的 Hook，包括安全 Hook。Plugin
成员必须使用 `<plugin-id>/<local-id>` 限定 ID，`plugins` 的 `[]` 不启用任何 Plugin，
显式子集只启用该子集，单独的 `["*"]` 在租用的 Generation 中物化全部 Plugin；`"*"` 不能与
其他 ID 混用。`agent.yaml` 不再有 `hooks` 字段：任何带 `hooks` 的记录都算不可加载。
控制面启动时会把用户目录里的这种记录退休到 `.staging/` 并告警，同名系统专家继续生效；
读取路径也会先做同样的退休，因此 `agent list`、专家详情和头像都不会被一条读不出来的
记录拖垮（见数据目录指南）。
`tools` 只接受 Runtime 内置工具 ID；MCP 贡献的工具随 `mcps` 选择，把 MCP 工具 ID 写进
`tools` 会返回 `422 validation_error`。执行模式只有一个字段：
`metadata.execution.requested_mode`（`host`/`sandbox`，默认 `host`）；旧
`metadata.runtime.requested_mode` 和「按安全策略自动选择」的 `auto` 都已退休，写入返回 422。
PATCH 省略 `model`
表示保持不变，显式 `null` 会被拒绝。头像使用 `/agents/{agent_id}/avatar` 子资源，图片保存在对应 Agent
目录中，并随 Agent 一起删除。响应还包含 `available` 和
`unavailable_reason`，以说明所选 Provider 是否存在且已配置。Run 使用 Session 所绑定 Agent 自己的 Provider、
模型和 MCP 能力，不接受旧式平面模型覆盖。

## 受控 Execution 生命周期

Execution API 管理由 Security Runtime 策略约束的 Agent 子进程，和对话 Run 是两类不同资源。
HTTP 调用方只能选择运行模式、受限环境变量和端口，不能提交 `command`、`entrypoint`、`cwd`、
包摘要、配置摘要或额外 sandbox 参数；这些值由已安装 Agent 包和不可变安全策略决定。未知字段
会返回 `422 validation_error`。

### 启动 Execution

```bash
curl -sS -X POST "$BASE_URL/api/v1/agents/AGENT_ID/executions" \
  -H 'Content-Type: application/json' \
  -d '{
    "requested_mode": "host",
    "env": {"LOG_LEVEL": "info"},
    "target_port": 8080,
    "listen_port": 0
  }'
```

成功返回 `201 Created`。请求字段如下：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `requested_mode` | string | `host` 或 `sandbox`；默认 `host`；已退休的 `auto` 返回 `422 validation_error` |
| `env` | object | 最多 32 项；变量名不能为空、不能含 `=` 或 NUL，名称最长 128，值最长 8,192 |
| `target_port` | integer/null | `1..65535`，启用受控代理目标端口 |
| `listen_port` | integer/null | `0..65535`，必须与 `target_port` 同时提供；`0` 表示自动分配 |

策略会过滤 `env`，只把允许列表中的变量传给子进程；Runtime 自行注入的身份变量不能被覆盖。
响应 `data` 包含 `instance_id`、`agent_id`、`backend`、`pid`、`cgroup_path`、`unit`、`state`、
`lease_id`、`started_at`、`sandboxed`、`container_id`、`reason` 和 `proxy_port`。
`state` 取 `STARTING`、`RUNNING`、`STOPPING`、`STOPPED`、`FAILED` 或 `REVOKED`。

### 查询、停止与撤销

```bash
curl -sS "$BASE_URL/api/v1/executions"
curl -sS "$BASE_URL/api/v1/executions/INSTANCE_ID"
curl -sS -X POST "$BASE_URL/api/v1/executions/INSTANCE_ID/stop" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"operator requested"}'
curl -sS -X POST "$BASE_URL/api/v1/agents/AGENT_ID/revoke" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"security revoke"}'
```

停止单个实例和撤销 Agent 的执行权限均返回 `{"data":{"accepted":true}}`。`reason` 必须为
1–1,024 字符；stop 默认 `requested`，revoke 默认 `security revoke`。不存在的实例返回 `404`。
revoke 会停止该 Agent 的活动实例并撤销其租约，后续能否再次启动由安全策略决定。

## 接口参考

本节按资源列出调用方法和关键参数。没有单列成功状态的读取、更新和控制操作均返回 `200`；
创建 Agent、Session、Provider、Package、Plugin、MCP Server 和 Execution 返回 `201`，创建 Run
返回 `202`，成功删除资源或头像返回 `204`。

### Agent 与头像

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/v1/agents` | 获取 Agent 列表 |
| `POST` | `/api/v1/agents` | 创建 Agent |
| `GET` | `/api/v1/agents/{agent_id}` | 获取 Agent |
| `PATCH` | `/api/v1/agents/{agent_id}` | 部分更新 Agent |
| `POST` | `/api/v1/agents/{agent_id}/restart` | 重建当前 Agent 的托管 Worker，应用已保存策略；有在途任务返回 409，启动失败返回 503 |
| `DELETE` | `/api/v1/agents/{agent_id}` | 删除 Agent |
| `POST` | `/api/v1/agents/{agent_id}/avatar` | 上传或替换头像 |
| `GET` | `/api/v1/agents/{agent_id}/avatar` | 获取头像二进制 |
| `DELETE` | `/api/v1/agents/{agent_id}/avatar` | 删除头像文件 |

创建请求示例：

```json
{
  "id": "writer",
  "name": "Writer",
  "name_zh": "写作专家",
  "name_en": "Writing Expert",
  "description": "写作助理",
  "description_zh": "写作助理",
  "soul": "回答简洁准确",
  "model": {"provider": "openai", "name": "gpt-5"},
  "tools": [],
  "skills": [],
  "mcps": [],
  "plugins": [],
  "metadata": {}
}
```

`id` 可省略，由服务生成。PATCH 只修改提交的字段，`model: null` 被拒绝。`name` 是调用名，
`name_zh`/`name_en` 是可选展示名（分别给简体中文和英文界面），客户端按自己的语言取值、
为空时回退 `name`；`description`/`description_zh` 是同样的两份界面简介。`description` 只用于
界面展示，不进入 LLM 上下文，模型行为提示只读取 `soul`。新的 Agent 记录写成
`schema_version: 2`，与 AgentHub 的 Power 格式 v2 一致；旧版本 1 的记录仍可读取。
旧 `instructions` 字段已删除。Agent 响应包含
`id`、`name`、`name_zh`、`name_en`、`avatar`、`description`、`description_zh`、`soul`、`model`、
`tools`、`skills`、`mcps`、
`plugins`、`metadata`、
`available` 和 `unavailable_reason`。头像上传字段名为 `file`，最大 2 MiB，只接受
JPEG、PNG、GIF、WebP，并校验 MIME 与文件头。

`origin` 区分 `system` / `user` 存放位置，不代表只读。系统目录中的 Bibit 同样支持
PATCH、头像修改和 DELETE：编辑生成用户目录副本，删除则在用户目录留下
`agents/.hidden-<agent_id>` 标记。标记生效期间该专家不出现在列表和详情中，签名包本身
字节不变，服务启动时的 Starter 发布也不会把它带回来；用同一 ID 重新创建、安装或同步
Agent 会清掉标记并恢复。出厂专家目录由 root 拥有且每次启动由
`provision-system-agent.py` 收敛，因此删除只能靠用户侧标记表达。Worker 内仍只读挂载
专家配置，由控制端处理管理操作。

### Session 与事件历史

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/v1/sessions?page=1&page_size=50` | 按最近活动时间分页获取 Session；每行带 `blank`、`last_prompt_at`、`as_of_seq`（来自该 Session 的 `projection.json`，列举不折叠日志） |
| `POST` | `/api/v1/sessions` | 创建并绑定 Agent |
| `GET` | `/api/v1/sessions/search?q=关键词&limit=20` | 跨 Session 检索会话正文（大小写不敏感子串），命中带消息锚点 `seq` |
| `GET` | `/api/v1/sessions/{session_id}` | 获取 Session |
| `PATCH` | `/api/v1/sessions/{session_id}` | 修改标题 |
| `DELETE` | `/api/v1/sessions/{session_id}` | 删除 Session、Run 与整个 `sessions/<id>/` 目录 |
| `POST` | `/api/v1/sessions/{session_id}/prompt` | 持久接收下一步骤或下一轮用户输入 |
| `POST` | `/api/v1/sessions/{session_id}/generate-title` | 立即写入兜底标题并异步生成模型标题 |
| `POST` | `/api/v1/sessions/{session_id}/clear-context` | 保留历史并切换上下文线程 |
| `GET` | `/api/v1/sessions/{session_id}/history?max_messages=50` | 读取 Session 最新显示窗口（Run 流分片已折叠）；用 `before_seq` 向前翻页 |
| `GET` | `/api/v1/sessions/{session_id}/events?max_messages=50` | 读取同一窗口的原始事件，窗口内 `seq` 连续；用于补洞与恢复 |
| `GET` | `/api/v1/sessions/{session_id}/events?from_seq=N` | 游标读：从 `N` 起返回未折叠的原始事件，页内 `seq` 连续；窄页增量补洞 |
| `POST` | `/api/v1/sessions/{session_id}/attachments` | 上传附件到 Agent workspace（multipart：单文件 `file`，或 `files` + `relative_paths` 目录）；返回工作区相对路径 |
| `GET` | `/api/v1/sessions/{session_id}/attachments/{name}` | 读回已存储的附件字节，供控制台预览 |
| `GET` | `/api/v1/sessions/{session_id}/vision` | 回答该 Session 当前模型能否读图，供控制台决定是否提示 |

创建请求为 `{"agent_id":"实际专家ID","title":"新对话","metadata":{}}`；PATCH 请求为
`{"title":"接口设计讨论"}`。Session 创建后固定绑定 Agent。原始事件以 Session 内连续
`seq` 持久化，`/events` 因此是补洞与恢复的契约：窗口内 `seq` 连续，客户端按 `seq` 去重。
`/history` 是同一窗口的显示读模型，只服务于历史展示：相邻、同一次 Run 的
`assistant/text.delta` 与 `assistant/reasoning.delta` 折叠成一条，保留首条 `seq`，正文拼接
进 `data.text`，被折叠的区间记在 `data.seq_range`。折叠后的 `seq` 只保证严格递增，实时游标
必须取 `data.seq_range` 的末尾，否则窗口末尾的流式分片会被误判成缺口。事件页还返回
`hidden_run_ids`，用于标记生成标题等内部 Run；客户端保留这些 Run 的原始事件以维持 `seq`
连续，但不会把它们投影到 Chat 或 Trajectory。

`/sessions/search` 复用同一份显示折叠：匹配的是用户输入与助手正文的
`casefold()` 子串，命中的 `seq` 是该条消息在 `/history` 里的锚点，`end_seq` 是它覆盖到
的最后一条分片。客户端拿 `end_seq` 调 `/sessions/{id}/history?before_seq=end_seq+1`
就能把整条消息读回到窗口里并定位（用 `seq+1` 只会读回回答的第一段分片），不需要第二套
索引或全文扫描。检索范围与界面可见范围一致：reasoning、工具收发、以及
`hidden_run_ids` 里的内部 Run 都不产生命中。`q` 只接受 1–200 字符，`limit`
默认 20、上限 50，`has_more` 表示还有更早的命中未返回。

`/events` 的第二读法是游标读 `from_seq=N`：给已经持有连续 `seq` 游标的客户端只取错过的
那一段。它返回未折叠的原始事件，页首 `seq` 恒等于请求值（客户端据此判定协议错误），一页
最多 512 条且不超过 4 MiB，`has_more` 表示还有更新的增量，客户端续读时把 `from_seq` 推到
已收到事件的最后一条。该模式与 `before_seq` 互斥（同时给出返回 422），并且只在 `/events`
有效：`/history` 会把 `seq` 重写成折叠后的序号，收到 `from_seq` 直接返回 422。注意两种读
法复用同一个 `has_more` 字段但方向相反：窗口读写的是「还有更早的历史」。

#### 通过 API 发送消息并临时选择 Skill

这不是独立的“执行 Skill”接口，而是在正常的会话输入请求中附带 `skill_ids`。
以下示例中的地址、专家 ID、技能 ID 和会话 ID 均须替换为实际值。

**1. 查询专家与可用技能。**

```bash
BASE_URL=http://10.0.91.146:9899
curl -sS "$BASE_URL/api/v1/agents"
curl -sS "$BASE_URL/api/v1/capabilities/skill"
```

从各自响应的 `data` 列表获取实际 `id`。技能应为 `enabled`、`assignable` 可用的条目。
`global.skills/review` 仅为示例，不保证已安装，也不是技能 ID 必须使用的前缀。
不要使用技能显示名称代替其 ID。条目还可能带 Power 格式 v2 的展示字段
`name_zh`、`name_en`、`description_zh`：客户端按自己的界面语言取值，字段为空时
回退到 `name` / `description`，请求里始终用 `id`。

**2. 创建绑定专家的会话；已有会话可跳过。**

```bash
curl -sS -X POST "$BASE_URL/api/v1/sessions" \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"实际专家ID","title":"代码审查"}'
```

记录响应中的 `data.id`，作为下面的 `SESSION_ID`。
专家已经由会话绑定决定，`/prompt` 请求中不再传 `agent_id`。

**3. 向会话发送实际任务，同时指定技能。**

```bash
SESSION_ID=替换为创建会话返回的ID
curl -i -X POST "$BASE_URL/api/v1/sessions/$SESSION_ID/prompt" \
  -H 'Content-Type: application/json' \
  -d '{
    "message_id": "review-msg-001",
    "content": "请审查这段代码：\ndef divide(a, b):\n    return a / b",
    "attachments": [],
    "skill_ids": ["global.skills/review"]
  }'
```

| 字段 | 必填 | 用途 |
| --- | --- | --- |
| `message_id` | 是 | 客户端生成的消息标识；同一条消息的重试必须复用，新消息使用新 ID |
| `content` | 是 | 实际任务正文，不需要添加 `/skill` 指令 |
| `attachments` | 否 | 附件信息；没有附件可省略或传 `[]` |
| `skill_ids` | 否 | 临时选择的技能 ID 数组，可同时选择多个；省略或 `[]` 表示不额外选择技能，不会移除专家原有技能 |

`message_id` 长度为 1–128，只允许字母数字、点、下划线和连字符；`content` 长度为
1–16,000。可选的 `skill_ids` 最多包含 100 个全局 Skill ID；ID 会去除首尾空白、按首次
出现顺序去重，空 ID 返回 `422 validation_error`。这些 Skill 只临时加入处理该条输入的
Run，不修改 Session 绑定的 Agent，也不扩大 Agent 的 Tool、MCP、Hook 或模型权限。若 Skill
在执行前被删除或禁用，该 Run 以 `skill_unavailable` 失败，而不会静默忽略。

当前实现限制：默认引擎临时合并所选 Skill 后，只将通过扫描的技能 `description`
加入系统提示词，尚未自动加载完整的 `SKILL.md` 正文（`instructions`）。
因此接口接收技能 ID，不等于模型已读取并执行完整技能步骤；这是当前行为，不是完整技能执行的保证。

**4. 接收提交结果，并单独查询执行结果。**

成功接收返回 HTTP `202`，示例：

```json
{
  "data": {
    "accepted": true,
    "message_id": "review-msg-001",
    "status": "claimed",
    "run_id": "本次执行标识"
  }
}
```

`202` 仅表示输入已接收，不代表模型执行成功，也不包含最终回答。
通过会话事件查看文本输出和最终状态：

```bash
curl -sS "$BASE_URL/api/v1/sessions/$SESSION_ID/events?max_messages=50"
```

关注 `assistant/text.delta`、`run/completed` 和 `run/failed`，根据事件的 `run_id`
区分会话中的不同任务；需要持续接收时可使用 `WS /api/v1/events/mux`。
技能不存在或不可用时，也可能先收到 `202`，随后执行以 `skill_unavailable` 失败，
不能只根据提交接口的 HTTP 状态判断任务成功。

输入进入 Session 的 `next-step` FIFO：活动执行在安全步骤边界按序 claim
当时已有的完整批次，并在同一内部执行和 thread 中继续；若提交时已错过活动窗口或 Session
空闲，服务端会启动新执行处理持久输入。带 `skill_ids` 的输入不会进入已经构建完成的活动
engine，而会等待活动 Run 结束后启动新 Run，以保证所选 Skill 确实进入该 Run 的系统上下文。
成功返回 `202`，其中 `data.status` 为 `staged` 或
`claimed`，并包含原 `message_id` 和当前内部执行标识。
网络失败重试必须复用同一 `message_id`；相同内容返回原结果，不同内容返回
`409 session_input_conflict`。重试时正文、附件和技能选择均应保持不变；修改技能选择也属于不同请求。
参数格式不合法返回 `422`。所选 Skill 不永久绑定专家；下一条消息仍需额外选用时，再次传入 `skill_ids`。
输入在 `agent/inbox/spliced` 持久化，claim 后才以
`user/message` 进入对话历史。

#### 在聊天界面使用 `/skill` 和 `@专家`

这两个操作都在 Web Console 的聊天输入框中使用，不是终端里的 `bianbu-agent` 命令。
`/skill` 用于“这次任务使用哪些技能”，`@专家` 用于“让哪个专家处理这段对话”。

**使用公共 Skill：**

1. 在输入框单独输入 `/skill` 或 `/skills`，按 Enter 打开技能选择器；两者等价。
2. 搜索技能名称、ID 或描述，点击选择一个或多个技能。列表来自当前 Runtime 已安装、启用且可选择的公共技能，
   不是远程技能商店；即使当前专家的配置未包含该 Skill，也可以临时选择。
3. 关闭选择器，输入实际任务，再点击发送。输入栏旁会显示已选技能标签；发送前可点击标签上的 `×` 取消选择。

例如：先输入 `/skill`，选择一个已安装的代码审查技能，再发送“请审查下面这段代码”。
不需要把 `/skill 技能名` 拼在任务正文前，也不需要修改专家配置。

选择只随下一条被服务端接受的消息发送，接受成功后标签自动清空；请求被拒绝或网络失败时保留选择，方便重试。
它不会永久绑定到专家或后续消息，也不会扩大工具、MCP、Hook 或模型权限。
如果已有任务运行，携带 Skill 的新输入会等待当前 Run 结束后执行；不会把技能插入正在运行的模型上下文。
列表为空时，先确认该技能已经安装、启用且可被选择；执行前被删除或禁用的技能会报 `skill_unavailable`。

**选择专家：**

1. 在输入框输入 `@`，专家候选列表会出现在输入栏正上方，不会展开历史侧栏。
2. 继续输入专家名称或 ID 筛选，例如 `@Bibit`，再点击目标专家；也可以使用 ↑ / ↓ 移动高亮、Enter 或 Tab 确认、Esc 关闭。方向键会跳过不可用专家，焦点保持在输入框；确认专家不会直接发送消息。
3. 确认当前专家名称，输入任务并发送。必须从列表中完成选择，单纯在正文写一个名字不代表已经切换专家。

新对话优先选择可用的 `BibitEdgeAssistant`，不依赖它的随机 ID，也不设置 Runtime 默认 Agent；
Bibit 不存在或不可用时显示专家选择器。历史会话统一按更新时间排列，不按专家分组。

- 选择当前会话的专家：继续使用原会话。
- 选择其他专家：进入该专家的新对话草稿，保留输入文字、附件及临时 Skill；首次发送才创建新会话。
  原会话仍绑定原专家，不复制旧历史，也不会停止原来正在运行的任务。
- 按 Esc：只关闭专家选择器；选择器关闭后，原有 Esc 停止任务快捷键才生效。

例如：正在和 Bibit 对话时，输入 `@` 并选择“写作专家”，再发送“帮我写一封邮件”，
邮件任务会进入写作专家的新会话，原来的 Bibit 对话仍保留。
两种操作可以组合：先用 `@` 选专家，再用 `/skill` 选公共技能，最后发送任务。

**其他客户端的 API 接入：**

`/skill` 和 `@` 是前端交互，不应直接作为控制指令塞进 `/prompt` 的 `content`。
技能列表通过 `GET /api/v1/capabilities/skill` 获取，将选中的实际 ID 放入上文的 `skill_ids`。
专家列表通过 `GET /api/v1/agents` 获取；切换专家时使用目标 `agent_id` 调用
`POST /api/v1/sessions` 创建新会话，再向新会话的 `/prompt` 发送任务。
不要尝试修改既有 Session 的专家绑定，也不要把显示名称当作 Agent ID。

首条消息被服务端接受后，Console 另行提交标题请求：

```bash
curl -sS -X POST "$BASE_URL/api/v1/sessions/demo-session/generate-title" \
  -H 'Content-Type: application/json' \
  -d '{"message_id":"demo-message-001","content":"Change direction"}'
```

接口立即返回 `202` 并持久化一个确定性兜底标题；模型标题在后台以无工具、无 Agent
checkpoint 的独立请求生成。标题请求最多读取 4096 UTF-8 字节、最多生成 64 token，60 秒
超时。失败或超时只保留兜底标题，绝不阻塞正式对话；晚到结果也不会覆盖用户显式改名。

实时下行是两条 Host 级 WebSocket：`WS /api/v1/events/mux` 传输所有 Session 的事件，
`WS /api/v1/events/host` 传输完整会话状态列表。两条连接在页面生命周期内保持，切换会话不会
重建连接。

### Session 执行控制

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/v1/sessions/{session_id}/prompt` | 提交首次输入或 steer |
| `POST` | `/api/v1/sessions/{session_id}/cancel` | 停止当前执行 |
| `POST` | `/api/v1/sessions/{session_id}/approvals/{approval_id}` | 批准或拒绝待处理动作 |

状态恢复读取 Session 详情，历史与实时展示统一消费 Session 事件。

### Execution

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/v1/agents/{agent_id}/executions` | 按安全策略启动 Agent 子进程 |
| `POST` | `/api/v1/agents/{agent_id}/revoke` | 撤销 Agent 的活动 Execution |
| `GET` | `/api/v1/executions` | 获取全部 Execution 快照 |
| `GET` | `/api/v1/executions/{instance_id}` | 获取一个 Execution |
| `POST` | `/api/v1/executions/{instance_id}/stop` | 停止一个 Execution |

请求约束、响应字段和安全边界见前文“受控 Execution 生命周期”。

### Provider

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/v1/providers` | 列出脱敏 Provider |
| `POST` | `/api/v1/providers` | 创建 Provider |
| `GET` | `/api/v1/providers/{provider_id}` | 获取 Provider |
| `PATCH` | `/api/v1/providers/{provider_id}` | 部分更新 Provider |
| `DELETE` | `/api/v1/providers/{provider_id}` | 删除未被 Agent 引用的 Provider |
| `GET` | `/api/v1/providers/{provider_id}/models` | 使用保存的凭据发现远端模型 |

```json
{
  "id": "openai",
  "label": "OpenAI",
  "base_url": "https://api.openai.com/v1",
  "api_key": "sk-...",
  "parameters": {}
}
```

响应以 `configured` 和 `api_key_masked` 表示凭据状态，永不回传明文 Key，另有
`origin`（`system` 表示包内出厂 Provider，`user` 表示用户自建）用于区分出厂标记；
`record_kind` 对两者都是 `custom_provider`，不能用来判断出厂与否。PATCH 省略
`api_key` 表示保留，显式空字符串表示清空。删除仍被 Agent 引用的 Provider 返回
`409 provider_in_use`：`message` 里同时给出专家名和 Agent ID（只有 ID 是内容哈希，
用户对不上列表），`details.agents` 是同样的 `{id, name, name_zh, name_en}` 列表。先给这些专家
换模型或删除专家再删 Provider。删除不存在的 Provider 或专家是 `404 not_found`，
`message` 说明是哪一类资源、哪个 ID，`details` 为 `{kind, id}`（`kind` 是
`agent`／`provider` 这样的机器标识）。出厂 Provider 位于
只读的系统资源目录，删除它会在用户目录写下 `models/.hidden/<record_id>` 标记：
Provider 立即从列表和详情中消失，包内容不变，重新创建同一 ID 会清掉标记。Provider
ID 含保留字符时必须按路径段 URL 编码。

Provider 记录的落盘位置是 `~/.bianbu-agent/models/<provider 目录>/provider.yaml`：
目录名是 Provider ID 小写化、`:` 换成 `-`、其余非 `[a-z0-9._-]` 字符换成 `-` 的结果
（`custom:bibit` → `custom-bibit`，`ollama` → `ollama`），文件名固定为
`provider.yaml`。文件里的 `id` 是记录 ID（`custom-provider-bibit`、
`provider-ollama`），不是目录名 —— 按记录 ID 找目录会找不到；`~/.bianbu-agent`
是 0700 的用户私有目录，非该用户的账号需要 sudo 才能查看。

### Capability、Package 与 Plugin

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/v1/capabilities/{kind}` | 获取当前 generation 的有效能力目录 |
| `GET` | `/api/v1/packages/{kind}` | 列出已安装独立包 |
| `POST` | `/api/v1/packages/{kind}` | 从服务端本地路径安装包 |
| `GET` | `/api/v1/packages/{kind}/{package_id}` | 查看包信息 |
| `POST` | `/api/v1/packages/{kind}/{package_id}/verify` | 校验包完整性 |
| `DELETE` | `/api/v1/packages/{kind}/{package_id}` | 卸载包 |
| `GET` | `/api/v1/plugins` | 列出 Plugin 及其成员 |
| `POST` | `/api/v1/plugins` | 安装并激活 Plugin |
| `GET` | `/api/v1/plugins/{plugin_id}` | 查看 Plugin |
| `DELETE` | `/api/v1/plugins/{plugin_id}` | 删除 Plugin 并 reload Runtime |

Capability 的 `{kind}` 为 `tool`、`skill` 或 `hook`；Package 的 `{kind}` 还接受 `agent`。
能力目录描述“当前 Runtime 实际可用什么”，Package 列表描述“磁盘安装了什么”，两者不能互相
替代。Package 与 Plugin 安装请求均为：

```json
{"source":"/absolute/server/path/to/package","upgrade":false}
```

`source` 必须是服务进程可见的绝对本地路径，不能是浏览器路径、上传内容或符号链接。Package
的 `upgrade` 当前只支持 Agent；Plugin 当前不支持 `upgrade: true`。安装或删除 Plugin 后应
重新读取 Plugin、MCP 和 Capability 目录。

每个能力的 `description` 是它自己的说明（内置工具来自模型可见的 docstring，可能只有中文或
只有英文）。Runtime 自有的内置工具另外带一个按语言分组的界面简介 `summaries`
（如 `{"zh": "...", "en": "..."}`）：Console 与 Bibit 设置插件的「工具」页按当前界面语言取
对应的一条展示，悬浮时再显示完整文本。其他来源（用户工具、Plugin 工具、MCP 工具）的
`summaries` 为空对象，界面退回显示 `description`。

### MCP Server 与健康检查

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/v1/mcp-servers` | 立即读取 generation 与缓存健康快照 |
| `POST` | `/api/v1/mcp-health-checks` | 等待一次共享实时健康检查 |
| `POST` | `/api/v1/mcp-servers` | 安装托管命令 MCP |
| `DELETE` | `/api/v1/mcp-servers?id={mcp_id}` | 删除可移除 MCP |

安装请求为 `{"name":"Filesystem","command":"npx -y ..."}`。健康检查 POST 不接受 body。
删除使用查询参数，因为 MCP ID 可能包含 `/`；`removable: false` 或非空 `owner_plugin_id`
表示必须删除所属 Plugin。安装/删除失败可能返回 `mcp_activation_failed`、
`mcp_reload_failed` 或 `mcp_owned_by_plugin`。

### 本地模型

设备上的本地模型（意图 LLM 与向量模型）归 `spacemit-ai-gateway` 包所有，
`bibit-intent` 在 `GET /v1/models` 上转述 Gateway 的状态。Runtime 只读取并转述，
不下载、不加载、不卸载，因此本接口是纯只读资源，没有对应动作接口。
Runtime 包声明了对 `spacemit-ai-gateway` 的运行时依赖，安装后两个来源至少有一个可用。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/v1/local-models` | 读取 Gateway 本地模型状态（只读） |

响应 `data` 含 `source`（`bibit-intent` 或 `ai-gateway`）、`read_only` 与 `models`；
每个模型含 `model_type`（`llm`/`embed`）、`name`、`artifact`、`backend`、`path`、
`download.status` 和 `server.status`。`name` 是上游的模型标识（设备上 Intent 报的是
Gateway 条目 `bibit-llm` / `bibit-embed`），`artifact` 是 Gateway 注册路径里的具体模型文件
名（如 `glm-edge-1.5b-chat-q4_0.gguf`），界面按 `artifact` 展示；解析不到时为 `null`。
Intent 与 Gateway 都不可用时返回 `502 bad_gateway`，消息里带上两个来源各自的失败原因。
浏览器只渲染这些字段，不注入任何上游 HTML。

### Settings 与监控

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/v1/settings` | 读取脱敏 deployment 与 runtime 设置 |
| `PATCH` | `/api/v1/settings` | 修改其中一类设置 |
| `GET` | `/api/v1/agent-top` | 获取不含 prompt 的 Agent/Session/Run 聚合快照 |
| `GET` | `/api/v1/logs` | 读取当前日志文件的 SSE 快照 |

Settings PATCH 一次只能提交 `deployment` 或 `runtime`，不能混合：
`deployment.language` 只接受 `zh`（简体中文）或 `en`（英文）。

```json
{"deployment":{"language":"zh"}}
```

```json
{"runtime":{"agent_max_steps":20,"context_compression_threshold":0.5}}
```

`agent-top` 返回计数、按 Agent 聚合的活动/失败状态和最多 50 条最近 Run。日志事件形如
`{"type":"log","content":"..."}`，读完当前日志文件后连接自然结束，不是持续 tail。

## 配置与扩展资源

- **提供商**：提供商保存 `id`、`label`、`base_url`、`api_key` 和参数；响应只显示掩码后的
  密钥状态。通用 Model CRUD 与 Capability Binding HTTP API 已删除；Agent 直接保存结构化
  Provider ID 和远端模型名。
  `GET /api/v1/providers/{provider_id}/models` 使用已保存凭据发现兼容模型，但不会在响应
  中暴露密钥。删除仍被 Agent 引用的 Provider 返回 `provider_in_use`。这是纯资源 API，不接受
  基于 `action` 的模型请求。
- **MCP Server**：`GET /api/v1/mcp-servers` 立即返回当前 Runtime generation 和内存中的
  健康状态快照，不等待实时 MCP 进程启动。快照在 60 秒内有效；没有快照或快照过期时采用
  **stale-while-revalidate**：本次 GET 仍立即返回 generation 已记录的状态或旧快照，并在后台
  合并为一次实时检查。首次后台检查完成前 `health_checked_at` 为 `null`；完成后它是本次状态的
  UTC 检查时间。`POST /api/v1/mcp-health-checks` 不接受请求体，它会等待当前共享的实时检查
  完成，并以相同的 `{"data":[...]}` 集合 envelope 返回结果。Agent 页面中的“刷新 MCP 状态”
  按钮调用此资源，刷新期间不会禁用专家卡片或编辑器。`POST /api/v1/mcp-servers` 安装托管
  命令，`DELETE` 使用同一集合路径和必填查询参数 `id` 删除。查询寻址可无歧义地承载包含 `/`
  的 MCP ID，避免把外部 ID 解释为路径层级。Plugin 所属 MCP 只读，必须通过所属 Plugin 删除。
  每台 Server 除保留原始 `status` 外，还带一套生命周期字段，供所有界面用同一种语言说明它
  在做什么：`state`（`not_loaded` / `preparing` / `ready` / `degraded` / `failed` /
  `stopped` / `unknown`）、`phase`（`preparing` 期间为 `installing` 或 `starting`，
  其余为空）、`ready`（是否已握手可调用）和 `tools_count`。`state=unknown` 表示读不到
  Worker 的加载状态，而不是“没加载”；`not_loaded` 才表示已声明但没有 Worker 加载它。
  `detail` 仍然只承载可付诸行动的原因（例如准备失败的原文），进度类的解释由界面按
  `state` / `phase` 本地化，避免同一句话在服务端被写死成一种语言。
- **包和 Plugin**：`kind` 为 `skill` 或 `agent`。安装源必须是服务进程可见的
  绝对本地路径，且不能是符号链接。安装 Plugin 或包等同于允许受信任代码/配置进入宿主进程。
  Runtime 不再提供 Plugin Console 动作接口。`GET /api/v1/capabilities/{kind}` 的 `kind`
  接受 `tool`、`skill` 或 `hook`，返回当前
  不可变 Runtime generation 的有效能力目录，包括内置能力、Plugin 来源、版本和不可用原因；
  它是 Agent 编辑器使用的只读选择目录；Hook 目录列出当前 Generation 已加载的 Hook
  （安全 Hook 也在内），并提供来源、状态和不可用原因。Hook 目录只用于查看：Hook 属于
  Plugin，不能由 Agent 直接选择，安装 Plugin 只改变这个有效目录；
  Agent 是否实际使用其中贡献，由它的 `tools`、`skills`、`mcps` 和 `plugins` 选择字段决定。
  `/packages/{kind}` 只负责独立包生命周期，不能代替有效目录。
- **设置**：读取设置会脱敏部署配置。PATCH 一次只接受 `deployment` 或 `runtime`，不能同时
  更新两者。部署当前可修改 `language`；Runtime 设置包括上下文、步骤、搜索和自演进限制。
- **Agent 头像、Agent Top 和日志**：头像是 Agent 子资源，最大 2 MiB，仅接受 JPEG、PNG、
  GIF、WebP；`GET /agent-top` 返回不含 prompt 的 Agent/Session/Run 聚合快照；日志是读完
  当前日志文件即结束的 SSE 快照流。通用 `/files` 与 `/avatars` API 已删除。
- **工作区 HTML 产物下载**：控制台把指向工作区 HTML 文件的 Markdown 链接重写为
  `GET /api/v1/workspace/download?path=<相对路径>`。服务只解析 `data_dir/workspace` 下的
  常规文件，越界或符号混淆路径按 `not_found` 处理，单文件上限 16 MiB、超出返回
  `payload_too_large`。响应固定为附件（`application/octet-stream`、
  `Content-Disposition: attachment`、`X-Content-Type-Options: nosniff`、
  `Content-Security-Policy: sandbox`、`Cache-Control: no-store`），因此工作区产物不会作为
  同源页面执行。

## Provider API Capture

Provider API Capture 是独立的运维诊断资源：它观察 Agent Run 发出的真实聊天模型调用，不会
创建合成 Playground 请求，也不会改变普通 Logs 页面或引入全局 Debug 开关。目标始终是
一个已配置 Provider 的规范 ID，不用显示名或 `base_url` 匹配。未 Start 和非目标 Provider
都是零写入。

### 成功响应

五个操作成功时都是 HTTP `200`，并带 `Cache-Control: no-store`：

| 方法 | 路径 | HTTP | body 类型 |
| --- | --- | --- | --- |
| `GET` | `/api/v1/provider-capture` | `200` | `collection` |
| `PUT` | `/api/v1/provider-capture` | `200` | `collection` |
| `DELETE` | `/api/v1/provider-capture/records` | `200` | `collection` |
| `DELETE` | `/api/v1/provider-capture/target` | `200` | `collection` |
| `GET` | `/api/v1/provider-capture/{record_id}` | `200` | `detail` |

所有成功 body 使用 `{"data": <value>}`。`collection` 的 `data` 含当前 `provider_id`、固定
`limits` 和最多 100 条最新 `records`；`detail` 的 `data` 是一条完整但有界的记录。`PUT` 以
`{"provider_id":"openai"}` 开始或切换 capture。Stop/Clear 都返回最新 `collection` envelope：
Stop 保留记录；Clear 保留目标。

以下 collection 行是节选；`id`、`captured_at`、`stored_size`、input/output 字符数与两个完整
preview 对象始终发出，其他 summary 字段按记录内容可选：

```json
{
  "data": {
    "provider_id": "openai",
    "limits": {
      "max_list_records": 100,
      "max_preview_index_bytes": 65536,
      "max_record_bytes": 8388608,
      "max_retained_records": 1000,
      "max_storage_bytes": 104857600,
      "preview_bytes": 4096,
      "retention_seconds": 604800
    },
    "records": [
      {
        "id": "0123456789abcdef0123456789abcdef",
        "captured_at": 1786400000.0,
        "stored_size": 512,
        "provider_id": "openai",
        "model": "gpt-5",
        "status": "success",
        "agent_id": "default",
        "session_id": "session-id",
        "run_id": "run-id",
        "duration_ms": 120,
        "input_characters": 42,
        "output_characters": 128,
        "input_preview": {
          "head": "...", "tail": "...", "truncated": false,
          "omitted_bytes": 0, "bytes": 42, "characters": 42
        },
        "output_preview": {
          "head": "...", "tail": "...", "truncated": false,
          "omitted_bytes": 0, "bytes": 128, "characters": 128
        }
      }
    ]
  }
}
```

### 错误响应

错误 body 始终是
`{"error":{"code":"...","message":"...","details":{...}}}`，并同样带
`Cache-Control: no-store`。下表的场景、HTTP 和稳定 `error.code` 是 Provider Capture 的客户端
分支契约：

| 场景 | HTTP | `error.code` |
| --- | --- | --- |
| malformed_json | `400` | `bad_request` |
| put_schema_invalid | `422` | `validation_error` |
| provider_not_configured | `422` | `validation_error` |
| provider_not_found | `404` | `not_found` |
| record_id_invalid | `422` | `validation_error` |
| record_not_found | `404` | `not_found` |

`malformed_json` 指 `PUT` 的 JSON 无法解析；`put_schema_invalid` 包括未知字段、空值、首尾空白、
控制字符或超过 256 字符的 Provider ID。Provider 不存在时是 `404`，存在但凭据未配置时是
`422`。详情 ID 不是严格 32 位小写十六进制时是 `422`；语法有效但文件不存在、已被 retention
删除或 gzip/JSON 损坏而被跳过时，都属于表中的 `record_not_found` 场景：HTTP 是 `404`，
`error.code` 仍是 `not_found`。

### 生命周期、边界与存储

Start/Switch 选择会原子持久化且跨服务重启保留，直到 Stop 或下一次 Switch。切换立即影响之后
开始的调用；已经为旧目标选中的 in-flight 调用仍写完原记录。collection 只返回 preview，detail
才返回完整但有界的记录；列表项含安全摘要以及 input/output 最多前 4 KiB 与后 4 KiB。
每个 preview 都返回 `head`、`tail`、`bytes`、`characters`、`truncated` 和
`omitted_bytes`：头尾无重叠时 8 KiB 内可完整重建；若 UTF-8 分割边界仍有字节无法显示，
`truncated` 会为 `true` 且 `omitted_bytes` 精确记录缺失字节，不会静默丢失尾部。为抵御 NUL、
控制字符等 JSON 转义膨胀，sidecar 编码后最多 64 KiB；必要时头尾会对称缩短，并继续精确报告
`omitted_bytes`。

单条完整、脱敏后的 JSON 在 gzip 前最多 8 MiB；超出的值替换为含原始/省略字节数的显式
`truncated` 标记。过深结构会在写盘前替换为带类型的 `max-depth` 标记，随后以详情读取器的同一
严格规则验证最终 JSON。retention 是 7 天或 100 MiB，以先到者为准，并额外最多保留
1,000 个记录 pair；100 MiB 是 gzip、preview sidecar 与 capture 临时文件的总量，按完整 pair
从最旧记录开始删除。物理清理在 Runtime 启动时和运行期间定期执行，即使没有打开 Console
或产生新调用，过期文件和遗留临时文件也会被清理；关闭 Runtime 会先发出停止信号，等待进行中的维护 worker 完成，
已选 target 不受影响。

路径永远相对启动配置解析后的数据根，而不是硬编码仓库目录：目标在
`<data_dir>/logs/provider-capture/target.json`，每条记录在
`<data_dir>/logs/provider-capture/records/<record_id>.json.gz`，并有原子发布的有界列表索引
`<data_dir>/logs/provider-capture/records/<record_id>.preview.json`。有界、原子发布的
`<data_dir>/logs/provider-capture/records/index.json` 保存最多 1,000 个记录 pair 的顺序和大小。
collection 只读该 manifest 和最新 100 条 preview sidecar，不枚举无界 records 目录，也不解压
所有 gzip；损坏 manifest 会让 collection 暂时返回空列表，维护周期会扫描有界保留集并从有效
gzip 修复 manifest 与 sidecar。跨 Store 实例的文件锁串行化 pair 发布、索引替换和裁剪。
当前仓库本地配置是
`.bianbu-agent/config.yaml` 中的 `data_dir: .`，所以该配置的实例路径示例为
`.bianbu-agent/logs/provider-capture/`；Debian 配置默认数据根是
`~/.local/share/bianbu-agent`。

Store 在写盘前脱敏全部 HTTP header 值、凭据类键、Bearer/Basic、JWT、常见 API Key、长高熵
token，并把 bytes、二进制块与 data URL 换成大小标记；捕获或磁盘失败不会令 Agent Run 失败。
脱敏后的 prompt、模型输出、工具内容和业务数据仍可能包含敏感信息，只应在受信任环境中临时
启用。专用 Console 页面是 Monitoring > API Capture；它按需读取 detail，并以 `textContent`
显示捕获内容。

前端生命周期必须遵守以下规则：

| 场景 | 触发条件 | 必须行为 |
| --- | --- | --- |
| dedicated_page | `Monitoring > API Capture` | `独立页面，与 Logs 分离` |
| visible_single_flight_polling | `API Capture 页面可见期间` | `轮询 collection，且最多一个请求 in flight` |
| navigation_cleanup | `离开 API Capture 页面时` | `停止轮询并 abort Provider、collection、detail、mutation 请求` |
| generation_owned_mutation | `PUT/DELETE 进行中` | `generation 拥有 AbortController，成功后重新读取 collection` |
| open_detail_on_demand | `仅当操作员显式点击 Open 后` | `请求该条记录的 detail` |
| text_only_rendering | `全部 Provider 与 capture 内容` | `仅用 textContent 或文本节点渲染，绝不使用 innerHTML` |

可见页面约每秒轮询一次，但只能在前一个 collection 请求完成后安排下一次。离开页面必须清除
timer、中止请求、忽略旧 generation 的迟到响应，并关闭详情框。mutation 失败时保留最后一次
服务端确认状态；成功后立即重新读取 collection 对账。所有响应都有
`Cache-Control: no-store`，不得放入 Service Worker、浏览器持久缓存或查询缓存。

## 客户端实现参考

### JSON、204 与统一错误

```javascript
export class ApiError extends Error {
  constructor(status, error) {
    super(error?.message || `HTTP ${status}`);
    this.status = status;
    this.code = error?.code || "http_error";
    this.details = error?.details || {};
  }
}

export async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const isForm = options.body instanceof FormData;
  if (options.body != null && !isForm && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(path, {...options, headers});
  if (response.status === 204) return undefined;
  const contentType = response.headers.get("content-type") || "";
  const document = contentType.includes("application/json")
    ? await response.json()
    : null;
  if (!response.ok) throw new ApiError(response.status, document?.error);
  return document?.data;
}
```

PATCH 表单只发送用户实际修改的字段，不要把未加载或未触碰的字段自动序列化成 `null`。
Provider API Key 尤其应在空输入且未触碰时省略。删除资源的 `204 No Content` 不应尝试解析 JSON。

### 折叠 Session 历史与实时事件

首次打开 Session 时只读取最新 50 条完整消息；历史加载期间到达的 WebSocket
事件按 `seq` 暂存。历史安装完成后再合并暂存事件。实时重复事件直接忽略，出现序号缺口则重新
读取权威历史。历史与实时必须调用同一个 reducer，避免刷新和切换产生不同气泡。

发送请求后的三点等待占位只保留到第一条可见 assistant 事件。reasoning、content、Tool 或
approval 一旦形成真实回复气泡，占位立即移除，不能在回复下方保留第二个气泡。只有当前 think
阶段保持展开；content、Tool、`assistant/message.end`、approval 或 Run 终态结束该阶段时立即
折叠，用户之后手动开合的状态由增量渲染保留。

## 精确路由索引

下列代码块由 `tests/test_api_docs.py` 与应用最终 OpenAPI 逐项比较。每行是一个且仅一个已发布
的 HTTP 操作；新增、删除或改名资源时必须同时更新此索引。

```text
GET /api/v1/agent-top
GET /api/v1/agents
POST /api/v1/agents
DELETE /api/v1/agents/{agent_id}
GET /api/v1/agents/{agent_id}
PATCH /api/v1/agents/{agent_id}
DELETE /api/v1/agents/{agent_id}/avatar
GET /api/v1/agents/{agent_id}/avatar
POST /api/v1/agents/{agent_id}/avatar
POST /api/v1/agents/{agent_id}/executions
POST /api/v1/agents/{agent_id}/restart
POST /api/v1/agents/{agent_id}/revoke
DELETE /api/v1/auth/session
GET /api/v1/auth/session
POST /api/v1/auth/session
GET /api/v1/capabilities/{kind}
GET /api/v1/executions
GET /api/v1/executions/{instance_id}
POST /api/v1/executions/{instance_id}/stop
GET /api/v1/local-models
GET /api/v1/logs
POST /api/v1/mcp-health-checks
DELETE /api/v1/mcp-servers
GET /api/v1/mcp-servers
POST /api/v1/mcp-servers
GET /api/v1/operational-logs
GET /api/v1/packages/{kind}
POST /api/v1/packages/{kind}
DELETE /api/v1/packages/{kind}/{package_id}
GET /api/v1/packages/{kind}/{package_id}
POST /api/v1/packages/{kind}/{package_id}/verify
GET /api/v1/plugins
POST /api/v1/plugins
GET /api/v1/plugins/effective
DELETE /api/v1/plugins/{plugin_id}
GET /api/v1/plugins/{plugin_id}
POST /api/v1/plugins/{plugin_id}/rollback
GET /api/v1/provider-capture
PUT /api/v1/provider-capture
DELETE /api/v1/provider-capture/records
DELETE /api/v1/provider-capture/target
GET /api/v1/provider-capture/{record_id}
GET /api/v1/providers
POST /api/v1/providers
DELETE /api/v1/providers/{provider_id}
GET /api/v1/providers/{provider_id}
PATCH /api/v1/providers/{provider_id}
GET /api/v1/providers/{provider_id}/models
GET /api/v1/runtime/maintenance
GET /api/v1/runtime/status
GET /api/v1/runtime/workers
DELETE /api/v1/security/agents/{agent_id}/syscall-policy
GET /api/v1/security/agents/{agent_id}/syscall-policy
POST /api/v1/security/agents/{agent_id}/syscall-policy
GET /api/v1/security/syscall-audit/events
GET /api/v1/security/syscall-audit/status
GET /api/v1/security/syscalls/catalog
GET /api/v1/sessions
POST /api/v1/sessions
GET /api/v1/sessions/search
DELETE /api/v1/sessions/{session_id}
GET /api/v1/sessions/{session_id}
PATCH /api/v1/sessions/{session_id}
POST /api/v1/sessions/{session_id}/approvals/{approval_id}
POST /api/v1/sessions/{session_id}/attachments
GET /api/v1/sessions/{session_id}/attachments/{name}
POST /api/v1/sessions/{session_id}/cancel
POST /api/v1/sessions/{session_id}/clear-context
GET /api/v1/sessions/{session_id}/events
POST /api/v1/sessions/{session_id}/generate-title
GET /api/v1/sessions/{session_id}/history
POST /api/v1/sessions/{session_id}/prompt
GET /api/v1/sessions/{session_id}/vision
GET /api/v1/settings
PATCH /api/v1/settings
GET /api/v1/workspace/download
```
