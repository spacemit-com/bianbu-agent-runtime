# Security 插件

插件 `bianbu.security` 贡献 8 个命令 Hook（`resources/plugins/bianbu.security/hooks/`），
把 Runtime 的 Hook 事件一一映射到 securityd 的干预点（`securityd/interceptor.py`）。
安全策略、能力字典、DLP 规则和审计链都在 securityd 侧维护；本插件只做客户端适配，
不在本地复制策略。

## 启用方式

`bianbu.security` 是可选插件，开关只有一个：Agent 的 `plugins` 是否选择它。

- 选择 `bianbu.security`（或 `*`）：该 Agent 注册到 securityd，下面的 Hook、
  DLP、判权和审计全部生效。
- 未选择：Runtime 不注册该 Agent、不写 securityd 的任何表、不挂安全中间件、
  不执行 DLP。Worker 以 host 进程启动，等价于 `yolo + host`（正常模式）。

未选择插件的 Agent 不依赖 securityd；即使 securityd 未安装或不可达，它也照常启动。

只有在系统**第一次安装** Runtime 包时，包内自带的 Agent 才会以出厂默认 `yolo`
（对应 `host` 执行后端）启动。“包内自带”按包目录里是否存在该 ID 判定，因此即使
该 ID 有用户副本、在有效存储里显示为 `user`，首次安装仍会初始化它。安装、重装和
升级都会重新启动控制面并重新注册专家，但重装/升级不会改写任何已选插件 Agent 的
安全模式：控制台里选择的 `safe`/`sandbox` 都会保留。

没有选择本插件的 Agent 不在这套重置逻辑的范围内：它在启动时就从 securityd 注销，
控制台里原有的模式记录随之删除，执行方式固定为 `yolo + host`。这是“未选择插件就
完全不依赖 securityd”的直接结果——Runtime 不会再读取或保留它在 securityd 侧的设置。

```
Runtime Hook 事件   →  securityd 干预点       →  拒绝时
BeforeRun           →  agent_startup           →  终止本轮，模型与工具都不执行
AfterContext        →  input                   →  终止本轮，模型调用前停下
BeforeModel         →  pre_model_call          →  终止本轮，模型不调用
AfterModel          →  post_model_call         →  终止本轮，已生成内容不发布
PreToolUse          →  pre_tool_call           →  终止本轮，工具不执行
PostToolUse         →  post_tool_call          →  终止本轮，结果不回到模型
AfterRun            →  output / agent_shutdown →  终止本轮，覆盖已完成的输出
OnError             →  （无独立切面）           →  终止本轮，覆盖原始失败原因
```

所有 Hook 事件点都能终止 Run。事件名只表示 Hook 在什么时候运行，不表示它能否否决。
统一路径是：插件抛 `SecurityDenied`（`lib/errors.py`，继承 `PermissionError`），
命令 Hook 协议把它编码成 `decision: deny`，宿主（`bianbu_agent/plugins/host.py`）
把任何 `PermissionError` 都当作拒绝向上抛，`RunManager` 用
`classify_run_exception` 把它投影成 `security_denied`。

命令 Hook 自身崩溃或超时同样按失败关闭处理，在任意事件点都抛
`HookDenied`（`bianbu_agent/hooks/command.py`）。需要尽力而为的 Hook 必须在
`hook.json` 里显式声明 `on_timeout: skip`。

## Hook 职责

### security.before_run — 启动闸门

`BeforeRun` / matcher `*` / timeout 10s / `on_timeout: fail`

调用 securityd 的 `agent_startup`，询问这个 Agent 能否启动。返回 `BLOCK` 时抛
`SecurityDenied`（`tool="<run>"`、`capability="agent.start"`），整轮 run 被拒绝。

- 只回 `metadata.securityd = {stage, agent_id, decision}`，不注入上下文。
- securityd 不可达或策略加载异常时返回 `None`，不阻断启动（fail-open）。
- 拒绝发生在模型调用之前，因此表现为本轮没有产出任何内容。

### security.after_context — 输入 DLP

`AfterContext` / matcher `*` / timeout 15s / `on_timeout: fail`

把用户本轮输入（`run["request"]`）送 securityd 的 `input` 切面做 PII 扫描，
按 DLP 策略返回 allow / confirm / deny / mask。

- `BLOCK` 时直接抛 `SecurityDenied`（`capability="input.scan"`）终止本轮，
  不再写 `deny_run` 让下游补拦。
- `MODIFY` 时把脱敏后的文本写入 `dlp_masked_input`，交中间件应用。
- 只扫用户输入；模型输出与工具参数分别由 `after_model` / `before_tool` 负责。

### security.before_model — 模型调用前

`BeforeModel` / matcher `*` / timeout 15s / `on_timeout: fail`

三层判断，任一 `BLOCK` 都抛 `SecurityDenied`（`capability="model.invoke"`）：

1. 兼容旧标记：`metadata.security_denied=True` 且非 YOLO → 拒绝。
2. 调 securityd 的 `pre_model_call`，扫描 prompt 与目标 `endpoint`（云端端点可按策略拒绝）。

`MODIFY` 时把 DLP 脱敏后的 prompt 写入 `dlp_masked_prompt`。
securityd 不可达时返回 `None`（fail-open）。

### security.after_model — 模型输出后

`AfterModel` / matcher `*` / timeout 15s / `on_timeout: fail`

把模型本轮输出（`metadata.response` 或 `metadata.output`）送 `post_model_call`
切面扫描：

- `BLOCK` → 调 `bridge.audit()` 记一条 `hook:post_model_call` 审计，再抛
  `SecurityDenied`（`capability="model.output"`）终止本轮，已生成的内容不发布。
- `MODIFY` → 写 `dlp_masked_output`。

### security.before_tool — 工具判权（主战场）

`PreToolUse` / matcher `*` / timeout 35s / `on_timeout: fail`

顺序：

1. **兼容标记**：`security_denied=True` → 拒绝；`requires_approval=True` 且未
   `approval_granted` → 拒绝（YOLO 模式跳过）。
2. **解析 capability**：`lib/mapping.py` 的 `resolve_capability()`。三态模型下
   策略键就是工具名，`scope` 恒为空；`clarify` 这类纯交互工具显式免判权（返回空 capability）。
3. **未映射工具**（`resolved is None`）：按 securityd 的 `unmapped_policy()`
   （默认 ask，不确定就交给人确认；开发期可用 `BIANBU_SECURITY_UNMAPPED=allow`
   放开，生产模式忽略该覆盖）。
   无论放行还是要求确认都写一条 `unmapped:*` 审计，避免新工具静默免判权。
4. **调 `pre_tool_call`**，传入 `{tool, capability, scope, args}`；远端 MCP 额外带
   `egress`（D-9：参数会发给第三方，DLP 据此提一档）。
5. **决策处理**：
   - `BLOCK` + `reason in {denied, handler_error, handler_timeout}` 或
     `dlp_action=block` → 明确拒绝，抛 `SecurityDenied`。
   - `BLOCK` 但属于「总线不可达」→ 走 `bridge.degrade()` 分档（见下）。
   - `CONFIRM` → 写 `requires_approval=True` 交 HITL 中间件。YOLO 模式下
     `bridge.invoke` 已直接返回 ALLOW，不会走到这个分支（代码里的
     `yolo_override` 是防御性兜底）。
   - `MODIFY` → 把脱敏后的参数写入 `dlp_masked_args`（Hook 层改不了参数本身）。
   - 会话里有可还原句柄时，把真实参数写入私有 `tool_arguments` 字段；它不进入
     run metadata，只由工具适配层应用。

`CONFIRM` 不在 Hook 内阻塞：这条 Hook 链由 HITL 中间件在 `after_model` 阶段
执行一次，`requires_approval` 直接决定是否弹审批卡。工具真正执行时不再重跑
这条链，所以一次工具调用只有一次 `CheckPermission`；审计里
`hook:pre_tool_call` 与它内部 `sec_capability` 产生的 `check_permission`
是同一条链的父记录与子记录，不是两次判权。

批准只对本次调用有效：Runtime 只在审计里写一条 `ask` +
`approved=true`（审计页显示为“你批准了 …”），不会写 `session_trust`。
因此同一会话里下一次调用同一个工具仍会重新弹卡；需要长期免确认时，把该
工具的策略改成 `allow`，而不是复用一次批准。

修改单个工具的 `allow` / `ask` / `deny` 只写一条策略行（`capabilities.policy.v1`
或 `capabilities.update.v1`），不会停 Worker、不改变沙箱模式，也不需要重启：
下一次工具调用的 Hook 链会重新问 securityd。

### DLP 替身与工具还原

敏感值只允许以**自然替身**形式进入模型上下文。替身与真值同类型、格式合法
（姓名、手机号、身份证、邮箱、银行卡、地址），而不是 `[PII-…]` 标记；模型
不应从文本看出发生过替换。工具仍要拿到原值，因此在**工具执行前**做一次
可信还原；模型永远看不到还原后的明文。

同一会话内，值到替身是稳定的、确定性的：

- securityd 的 session 映射按 `(pii_type, value)` 去重：同一会话里同一个值复用
  同一个替身，不同值分配新替身。新替身与已有替身或真实值碰撞时，按
  `counter` 重试，直到得到一个本会话未占用的值。
- 如果用户后来把某个旧替身当作真实值输入，旧映射保留（模型已经在前面的
  轮次里见过它），新真实值分配另一个替身。因此映射的 key 与 value 集合
  可以重叠，还原必须单遍完成。
- `pre_tool_call` 扫描前先用 session 映射把句柄还原成真值，只用于策略判断；
  审计载荷仍然是句柄，不复制明文。不还原就等于扫不到 PII，工具参数的
  CONFIRM/DENY 会静默失效。
- `before_tool` 的还原结果走 `ContextPatch.tool_arguments` 私有字段，不写
  `metadata`（metadata 会投影到 run result / API / 观测面）。
- `after_tool` Hook 收到的参数仍是模型侧的句柄：Hook 观察的是模型这次调用，
  明文只属于工具本身。否则第三方 Hook 与 Hook 失败日志都会拿到明文。
- Runtime 使用与 securityd 相同的确定性生成器（`natural_stand_in` /
  `replace_stand_ins`）。旧的 `[PII-phone-R<12 hex>]` 句柄仍可识别，用于兼容
  升级中的会话，但新脱敏不再产生它们。未知、过期或跨 session 的旧句柄
  fail-closed，按 DLP 拒绝处理。
- 模型输出的流式 delta 在发布前由 Run 的私有映射还原；一个替身被拆在两个
  chunk 之间也能正确还原。`after_tool` 在引擎内运行，它新增的映射通过
  `RunControl.dlp_stand_ins` 私有通道回到 Run 循环，不进入 run metadata。
- 工具结果和模型输出返回模型前仍按原策略脱敏；映射只存在进程内存。一个
  Run 结束**不**清映射，只有 Session 明确终止
  （`session_closed` / `session_deleted` / `agent_removed`）才清。Runtime
  引用默认 TTL 1 小时、单 session 最多 2048 条，可用
  `BIANBU_AGENT_DLP_REF_TTL` / `BIANBU_AGENT_DLP_REF_MAX` 调整。
- 非文本内容块没有可扫描的文本：模型请求里出现图片、音频、文档或内联
  `data:` 载荷时 fail-closed，不会原样外发。`read_image` 是这条规则的显式
  例外：图片字节交给 Agent 选定的视觉模型接收，可以是云端 Provider；发送前
  对 `question` 做 DLP 文本扫描，图片本身不再按端点位置拦截。
- 会话标题是 Agent 图之外的辅助模型调用，`prepare_title_request()` 在派发前
  对 system/input 做同一套 DLP；命中 deny/ask 时保留本地回退标题。

Agent 的安全模式直接决定执行后端，不再由「声明了哪些权限」推导：

- `safe`（安全模式）：运行在 sandbox（`bwrap`）中，逐工具策略、可读/可写
  范围、网络限制与系统调用黑名单都在这里落实；
- `yolo`（正常模式）：运行在 host 上，所有工具默认放行，跳过 DLP 扫描与
  审批，只保留审计。

所以切换模式一定会重建 Worker：host 进程不能原地变成沙箱，网络命名空间和
挂载表也在启动时固定。旧 Worker 仍在执行任务时接口返回 `409 agent_busy`，
避免在 Run 中途换边界。`auto` 只作为旧调用方的请求值，最终后端仍由
securityd 决定；未选择 `bianbu.security` 的 Agent 不经过 securityd，
Runtime 直接固定为 `host`。

该判定是四态：`ALLOW`、`ASK`、`DENY`、`UNKNOWN`，由 BEFORE_TOOL Hook 链给出，
HITL 中间件只消费这一份结果。securityd 明确返回 `ALLOW`
时直接放行，不再用工具的静态 `requires_approval`、`rm` 谓词或 memory-remove
谓词二次覆盖——否则策略页显示 allow，实际仍会弹审批。只有 `UNKNOWN`
（插件缺失、securityd 不可达、判定异常）才回退到这些静态契约；`BIANBU_SECURITY=0`
同样视为 `UNKNOWN`，让静态契约独自生效。

### security.after_tool — 工具结果后处理

`PostToolUse` / matcher `*` / timeout 15s / `on_timeout: fail`

把工具结果送 `post_tool_call` 切面扫描：

- `MODIFY` → `dlp_masked_result`。
- `BLOCK` → 抛 `SecurityDenied`（`capability="tool.result"`）终止本轮；工具已经
  执行，但结果不会回到模型，也不会出现在最终输出里。
- 结果为空或切面异常时补一条 `executed/ok` 审计，保证审计链能看到工具完成。

### security.after_run — 终态

`AfterRun` / matcher `*` / timeout 15s / `on_timeout: fail`

两件事：

- `output` 切面扫描最终输出，`BLOCK` 时抛 `SecurityDenied`
  （`capability="output.scan"`），覆盖本轮已经算出的结果；`MODIFY` 时写
  `dlp_final_output`，并通过私有 `user_visible_text` 把还原后的文本交给
  Runtime。该文本只在本会话已有的值上保持真实：本会话从未处理过的新 PII
  仍按当前策略脱敏。
- `agent_shutdown` 以 `run_complete` 调用时**不清**映射，只回写
  `pii_mappings_retained`。Run 只是用户的一轮对话；清掉映射后，下一轮模型
  引用上一轮的替身就无法还原了。Session 删除时，控制面通过 Worker RPC
  `session.terminate` 触发 `session_deleted`，Worker 和 securityd 才真正
  清零（映射含明文，只存在内存）。

### security.on_error — 错误审计

`OnError` / matcher `*` / timeout 10s / `on_timeout: fail`

纯观察者：把 `error:{tool}`、`on_error`、错误摘要（截断 200 字符，避免撑大哈希链）
写进审计，返回 `None`。它本身不判断；但和其它事件一样，若它抛出
`PermissionError`，该拒绝会覆盖触发它的原始失败原因。

## 降级与开关

securityd 不可达时 `bridge.available == False`：

| 场景 | 行为 |
| --- | --- |
| `before_run` / `after_*` / `on_error` | 直接返回 `None`，fail-open |
| `before_tool` 中高危工具 | `bridge.degrade()` fail-closed 抛 `SecurityDenied` |
| `before_tool` 中低危工具 | 记 warning 后放行 |
| `BIANBU_SECURITY_FAIL_OPEN=1` | 强制全放行（仅开发用） |

高危清单见 `lib/bridge.py` 的 `HIGH_RISK_TOOLS`：`write_file`、`patch`、`terminal`、
`process`、`skill_manage`、`cronjob`、`send_message`、`send_email`、`ha_call_service`、
`rl_start_training`、`rl_edit_config`、`delegate_task`、`image_generate`。

其它环境变量：

- `BIANBU_SECURITYD_HOME`：securityd 源码位置（默认探测 `/usr/lib/bianbu-securityd` 等）。
- `BIANBU_AGENT_DLP=0`：不注册 DLP handler，只保留判权与审计。
- `BIANBU_AGENT_AUDIT_UDS_THRESHOLD`：超过该字节数的审计载荷走 UDS（默认 1024）。
- `BIANBU_AGENT_DLP_POLICY_TTL`：DLP 策略缓存秒数（默认 5）。
- `BIANBU_WORKER_MANAGED=1`：要求 Worker 身份，缺失时拒绝启动。
- YOLO 模式：`lib/bridge.py` 的 `invoke()` 在 `input`、`pre_model_call`、
  `post_model_call`、`pre_tool_call`、`post_tool_call`、`output` 六个切面上
  **直接返回 ALLOW，不调用任何本地 handler**——能力判权和 DLP 都被跳过，
  只写一条 `hook:<stage> ... mode=yolo;checks=skipped` 审计。
  `agent_startup` 与 `agent_shutdown` 不在旁路集合内，仍会执行（前者只加载
  DLP 策略，不扫描内容）。YOLO 的语义是「仅记录」：保留统计与审计，
  不执行 DLP、网络策略、配额判定、审批或运行限制。

## 与其它插件的关系

`bianbu.security.before_run` 与 `bianbu.memory`、`bianbu.bibit.intent` 的 `BeforeRun`
Hook 在同一个事件点。当前宿主对同一事件点的所有 Hook 并发启动、只按
`(priority, hook_id)` 合并结果，因此 `bibit-intent` 的 `priority: 200` **不保证**
它在安全裁决之后执行——端侧请求可能在 run 已被拒绝之后仍然发出。
`bibit-intent.before_run` 会真实触发设备侧动作，改动这里的事件调度时需要一并考虑。

## 与 overview-guide 的表述差异

`docs/overview-guide.md`（能力授权一节）仍写着 YOLO 下「DLP、审计和 securityd
fail-closed 保持启用」。这与当前实现不一致：2026-09-16 的 YOLO「仅记录」变更
（`08746119`）之后，YOLO 会跳过 DLP 扫描，只保留审计与统计。以本文档和
`resources/plugins/bianbu.security/lib/bridge.py` 的实现为准。
