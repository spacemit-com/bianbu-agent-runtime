# Bianbu Agent Runtime 单次请求性能报告

> 测量日期：2026-10-20
> 代码基线：`37f397d9`（`pyproject.toml` 0.1.178）
> 测量设备：SpacemiT K3，Runtime 监听 `127.0.0.1:9899`
> 被测请求：Console 输入「打开终端」
> 被测会话：`session_db8ff41b-88ab-4a91-8db9-9be8c26b6007`

本文回答一个具体问题：**用户输入「打开终端」后，1.954s 的耗时分别花在哪里，为什么**。
报告只覆盖这条端侧直答路径，不包含云端模型推理；需要模型的请求还要叠加首 token 与生成时间。

---

## 1. 结论

这条请求从 `user/message` 到 `run/completed` 共 **1.954s**。按关键路径可以完整拆成四段：

| 阶段 | 墙钟耗时 | 占比 | 主要成本 |
| --- | ---: | ---: | --- |
| 请求准入到第一个 Hook 启动 | 0.429s | 22% | Run 准入前的能力清单重扫、Worker RPC、调度与持久化 |
| `before_run` 三个 Hook 并发执行 | 0.706s | 36% | 三个 Python 子进程冷启动；最慢的是 `memory.before_run` |
| 发布 `direct_reply` | < 1ms | ~0% | Hook 已经给出答案，不进入 LangGraph |
| `after_run` 调度与执行（到 `run/completed`） | 0.819s | 42% | 含 5.6ms 调度空档；两个 Hook 仍是 Python 子进程冷启动，最慢的是 `memory.after_run` |
| **合计** | **1.954s** | **100%** | |

四段中 `direct_reply` 发布小于 1ms，因此占比按前三段与 `after_run` 窗口计算。

最重要的三个判断：

1. **轨迹里的 `Startup 0.44s` 不是 Worker 启动**。它是「本轮第一条用户消息」到「第一个 Hook 被调用」之间的空档，Worker 当时已经在运行。标签来自 `trajectory.js` 的一个兜底分支：本轮还没有任何记录时，第一段空档一律叫 `startup`。
2. **Hook 慢在进程启动和 import，不在业务逻辑**。同一个 `bridge.recall()` 在进程内只要 1.2ms，但把它放进 `sh -c` 子进程后，仅 `import bianbu_agent_sdk.memory` 就要 364–416ms。五个 Hook 合计贡献了 1.5s 左右的关键路径。
3. **Run 准入前每次都会完整扫描并校验能力清单**。`worker.py` 在取 Run 租约前调用 `reload_installed(handoff=True)`；K3 上这一步中位数约 **251ms**，其中技能清单被扫了两遍、校验了两遍。

---

## 2. 测量口径

### 2.1 被测请求

```text
session_id: session_db8ff41b-88ab-4a91-8db9-9be8c26b6007
run_id:     eead7ec4-20ae-4deb-93dd-65328f63c517
prompt:     打开终端
agent:      com.bianbu.627d97ad908c
result:     已启动应用: 终端
```

这条请求由 `bibit-intent.before_run` 命中规则路由，返回 `direct_reply`，因此：

- 不会等待 LangGraph 执行链完成（engine build 并行启动后被 detach）；
- 不调用云端模型；
- 不经过 `before_model` / `after_model` / `before_tool` / `after_tool`；
- 只执行 `before_run` 三个 Hook 和 `after_run` 两个 Hook。

### 2.2 数据来源

- 会话事件：`GET /api/v1/sessions/{session_id}/events`。响应里 `seq` 2–21 共 20 个事件，`base_seq=2`；持久化计数为 `durable_events=22`，差额来自 API 窗口起点之前的序号。`/tmp/ev.json` 是当时的原始响应。
- Hook 自报耗时：`hook/result.data.duration_ms`，由 `plugins/host.py` 用 `time.perf_counter()` 在 Hook 调用前后测量。
- 墙钟时间线：事件里的 `time` 字段（Unix 秒）。
- 进程内直调与子进程基准：在 K3 上用同一个 Runtime 解释器和同一份 Hook payload 运行。
- 38 个 Run 的准入空档统计：对 `user/message` 到下一条同 `run_id` 事件的间隔取中位数。

### 2.3 注意

- 同一 Hook 点的多个 Hook **并发执行**，因此各 Hook 的 `duration_ms` 不能相加，关键路径取最慢的那个。
- `duration_ms` 是 Hook 自身视角；墙钟窗口还包含事件发布、调度和结果合并的几毫秒。
- 测量时段 K3 上有一次 `systemd reload` 占用约 1201ms，属于环境干扰项，不应归因于 Runtime。
- 单次样本适合定位瓶颈，不用于承诺稳定尾延迟；38 个 Run 的准入空档用于交叉验证。

---

## 3. 端到端时间线

以 `user/message` 为 t=0：

| 相对时间 | 事件 | 说明 |
| ---: | --- | --- |
| 0.000s | `user/message` | 消息已持久化，Run 记录建立 |
| 0.429s | `hook/invoked` × 3 | `memory`、`security`、`bibit-intent` 三个 `before_run` Hook 同时启动 |
| 0.926s | `hook/result` security | `security.before_run` 完成，`decision=ALLOW`，自报 500.5ms |
| 1.072s | `hook/result` bibit-intent | 规则命中 `launch_app`，自报 638.6ms，其中端侧 HTTP `latency_ms=271.4` |
| 1.135s | `hook/result` memory | `memory.before_run` 完成，自报 724.8ms，是 `before_run` 的关键路径 |
| 1.135s | `assistant/text.delta` | 发布 `direct_reply`：「已启动应用: 终端」 |
| 1.141s | `hook/invoked` × 2 | `memory.after_run`、`security.after_run` 同时启动 |
| 1.761s | `hook/result` security | `security.after_run` 完成，自报 621.9ms |
| 1.953s | `hook/result` memory | `memory.after_run` 完成，自报 817.0ms，是 `after_run` 的关键路径 |
| 1.954s | `run/completed` | 终态落库并下行 |

时间线对应的原始事件序列：

```text
17.8987  user/message
18.3282  hook/invoked  memory.before_run
18.3284  hook/invoked  security.before_run
18.3285  hook/invoked  bibit-intent.before_run
18.8246  hook/result   security.before_run     500.5ms
18.9708  hook/result   bibit-intent.before_run 638.6ms
19.0341  hook/result   memory.before_run       724.8ms
19.0342  assistant/text.delta
19.0398  hook/invoked  memory.after_run
19.0420  hook/invoked  security.after_run
19.6596  hook/result   security.after_run      621.9ms
19.8518  hook/result   memory.after_run        817.0ms
19.8529  run/completed
```

### 3.1 关键路径闭合

```text
0.429s  准入空档
0.706s  before_run 窗口（18.3282 → 19.0341）
0.000s  direct_reply 发布
0.819s  after_run 窗口（19.0342 → 19.8529）
------
1.954s
```

四段严格闭合，说明这段时间没有隐藏的大块开销；优化必须直接作用于 Hook 或准入扫描。

---

## 4. 逐项归因

### 4.1 0.429s 准入空档

这段从 `user/message` 到第一个 `hook/invoked`，主要包含：

```text
Console POST /prompt
  └─ SessionInputBroker.prompt
      └─ RunBroker.submit
          └─ ControlRunManager.run / WorkerSupervisor.run
              └─ WorkerClient.run_start
                  └─ Worker.dispatch("run.start")
                      ├─ reload_installed(handoff=True)   ← 能力清单全量扫描与校验
                      └─ RunManager.run
                          └─ 取 PluginGeneration 租约
                              └─ 创建 Run 任务
                                  └─ before_run Hook
```

其中可以单独量化的是 `reload_installed(handoff=True)`：

| 子阶段 | K3 中位数 | 说明 |
| --- | ---: | --- |
| `_inventory_snapshot` 全量快照 | 139.3ms | 遍历 plugins、mcps、skills、agents，解析 YAML/JSON/SKILL.md |
| 技能快照（重复扫描） | 72.6ms | `skill_snapshot_provider` 又把技能目录扫了一遍 |
| `verify_snapshot` 全量 | 22.3ms | 校验全量清单的摘要与文件支撑 |
| `verify_snapshot` 技能 | 7.4ms | 技能清单再校验一遍 |
| Agent 包遍历 | 10.3ms | 计算 Agent 包内容摘要，参与 generation 签名 |
| 能力目录自旋锁 | 0.2ms | 文件系统锁等待 |
| **合计** | **约 251ms** | |

快照内部 profile 显示，YAML 解析是大头：一次准入会发生约 26 次 `yaml.load`，技能 loader 合计约 163ms。

因此 0.429s 可以粗略拆为：

```text
能力扫描与校验   ~0.251s
Worker RPC/调度  ~0.178s
```

后者包括 UDS 往返、Run 记录写入、租约与任务创建。它不是单点瓶颈，但会和能力扫描叠加。

38 个 Run 的同一空档统计：**中位数 0.419s，最小 0.004s**。最小值说明 Worker 热态时这段可以接近瞬时，0.4s 是能力重扫带来的常态成本，而不是固定的进程启动成本。

### 4.2 `before_run` 0.706s

三个 Hook 并发启动，但结果按 `(priority, hook_id)` 顺序合并。合并必须等最慢的 Hook，所以关键路径是 `memory.before_run` 的 724.8ms。

| Hook | 自报耗时 | 关键路径 | 主要工作 |
| --- | ---: | :---: | --- |
| `memory.before_run` | 724.8ms | 是 | 进程启动 + import memory SDK + 召回记忆/指令记忆 |
| `bibit-intent.before_run` | 638.6ms | 否 | 进程启动 + 规则路由 + `POST /v1/runs`（`latency_ms=271.4`）+ 启动应用 |
| `security.before_run` | 500.5ms | 否 | 进程启动 + import security 客户端 + `before_run` 判权，`decision=ALLOW` |

这里有两个容易误判的点：

- **不是三个 Hook 串行相加**。如果串行，会接近 1.8s；实际窗口只有 0.706s，因为它们是并发的。
- **`direct_reply` 不会取消其它 `before_run` Hook**。`bibit-intent` 在 1.072s 就已经拿到答案，但 Run 仍要等 `memory` 在 1.135s 完成合并，才能发布回复。这 63ms 是确定性的合并等待，不是故障。

### 4.3 `direct_reply` 发布

`RunManager` 在 `before_run` 返回 `direct_reply` 后直接发布 `assistant/text.delta`，不进入 LangGraph：

```text
before_run.direct_reply is not None
  ├─ detach 已经启动但没有用到的 engine build
  ├─ publish token
  └─ return RunResult(status="completed")
```

这段本身小于 1ms。它的意义是：这条请求的 1.954s 里**没有任何模型时间**，全部是运行时准备与 Hook。

代码里还保留了一段设计说明：engine build 与 `before_run` 并行启动，但在 `direct_reply` 路径上会被 detach，因为同步 builder 跑在工作线程里，取消并不能立刻中断它；让回复等它没有意义。

### 4.4 `after_run` 窗口 0.819s

从 `direct_reply` 发布（19.0342）到 `run/completed`（19.8529）共 0.819s，其中前 5.6ms 是调度空档，两个 Hook 在 19.0398 才启动。两个 Hook 并发，关键路径是 `memory.after_run`：

| Hook | 自报耗时 | 关键路径 | 主要工作 |
| --- | ---: | :---: | --- |
| `memory.after_run` | 817.0ms | 是 | 进程启动 + import memory SDK + 记录本轮记忆/检查点 |
| `security.after_run` | 621.9ms | 否 | 进程启动 + import security 客户端 + 输出扫描 + `agent_shutdown`，`pii_cleared=0` |

`after_run` 比 `before_run` 还慢，原因不是业务更复杂，而是：

- 两个 Hook 仍然各自起一个全新的 Python 进程；
- `security.after_run` 要做 output 切面扫描和 `agent_shutdown` 两次 securityd 交互；
- `memory.after_run` 要处理本轮记忆落库。

### 4.5 标题生成（log-only）

Console 在首轮之后还会请求一个标题。标题不再是 Run，而是 Session 日志里的两条状态事件：

```text
session/title         20.2259   （fallback：「打开终端」）
session/title/request 20.2260   （system + 本轮消息的 JSON 输入）
session/title/route   21.5920   （provider/model）
session/title         21.5921   （模型返回的标题，若成功）
```

因此：

- 标题不占用 Run 槽位，也不需要等前一轮结束；
- 标题不执行任何 `before_run` / `after_run` Hook，不会产生 `hook/invoked`；
- 标题请求失败或超时只意味着没有新的 `session/title`，fallback 仍是当前标题；
- 标题事件（`run_id=null`）与 Run 事件（`run_id` 有值）是轨迹系统的两种输入，Console 把前者渲染为独立的「标题」轮次，并展示请求上下文与 route。

旧版本中标题作为 `console_operation=generate_title` 的辅助 Run，会占槽位并在结束后多跑约 0.7s 的 `after_run` Hook；这部分开销现在已不存在。

---

## 5. 为什么轨迹显示 `Startup 0.44s`

`bianbu_agent/console/web/static/js/trajectory.js:156` 的逻辑是：

```js
function waitReason(previousSpan, nextSpan) {
    if (previousSpan == null) return 'startup';
    ...
}
```

本轮第一条记录是 `user/message`，它之前没有任何 span，所以 `user/message` 到第一个 `hook/invoked` 之间的 0.429s 被标成 `Startup`。

这个标签会让人以为是 Worker 冷启动，但实际不是：

- 事件里没有 `progress.phase=starting_agent`。该事件只在 `supervisor.py:1221` 的 `not supervisor.ready` 分支发布。
- Worker 进程的 `etimes` 在请求前后持续增长，没有重启。
- 38 个 Run 的同一空档中位数为 0.419s，最小 0.004s；真正的冷启动不会稳定落在 0.4s。
- 这段空档与 `reload_installed(handoff=True)` 的 251ms 能力扫描高度吻合。

更准确的名字是 **Run admission**（准入）或 **准备执行**。如果保留 `Startup`，应只在真的发生了 `starting_agent` 时使用；否则应显示为 `Admission`。

建议同时做两件事：

1. 前端把首个空档改名为 `Admission`，或在 tooltip 中注明「不是 Worker 启动」。
2. 后端在准入阶段发布细分事件，例如 `admission.snapshot.started/finished`、`admission.lease.acquired`，让轨迹能直接对账，而不是靠外部 profile 推断。

---

## 6. Hook 子进程的固定开销

### 6.1 启动方式

每个命令 Hook 都通过 `bianbu_agent/hooks/command.py:126` 启动：

```python
process = await asyncio.create_subprocess_exec(
    '/bin/sh', '-c', command, cwd=workspace, env=env, start_new_session=True,
    stdin=PIPE, stdout=PIPE, stderr=PIPE)
```

这意味着每次 Hook 都要支付：

```text
fork/exec /bin/sh
  └─ exec RUNTIME_PYTHON
      └─ Python 解释器初始化
          └─ import 插件脚本及其依赖
              └─ 读取 stdin JSON
                  └─ 执行业务
                      └─ 写 stdout JSON
                          └─ 退出
```

### 6.2 基准数据

| 场景 | K3 实测 |
| --- | ---: |
| `python -c pass` | 56ms |
| `import bianbu_agent_sdk.memory` | 364–416ms |
| 仅基础依赖（`asyncio/json/os/re`） | 211–224ms |
| security 插件 `from lib import get_bridge` | 248–261ms |
| `bianbu_security_client.list_agents()` | 155–220ms |

`import bianbu_agent_sdk.memory` 的内部导入耗时（有嵌套，不可直接相加）：

| 模块 | 耗时 |
| --- | ---: |
| `mem0` | 67ms |
| memory client | 167ms |
| `asyncio` | 107ms |
| types | 47ms |
| temporal | 24ms |
| ontology | 19ms |

### 6.3 真实 Hook 子进程端到端

用同一份 payload、同样的 `sh -c` + stdin 方式：

| Hook | 实测 |
| --- | ---: |
| `memory.before_run` | 396–421ms |
| `security.before_run` | 303–363ms |
| `bibit-intent.before_run` | 500–535ms |

### 6.4 进程内直调对比

| 操作 | 进程内耗时 |
| --- | ---: |
| `bridge.recall()` | 1.2ms |
| `settings()` | 1.1ms |
| `record_run_started` | 1.2ms |
| 端侧 `POST /v1/runs` | 182–219ms（`latency_ms≈211`） |

结论很直接：**memory/security 的业务逻辑是毫秒级，几百毫秒几乎全是进程启动与 import**。这也解释了为什么 `before_run` 的 724.8ms 比独立基准 396–421ms 更高：除了固定启动成本，还要叠加记忆后端召回、与其它 Hook 争抢 CPU，以及事件合并等待。

---

## 7. 其它观测

| 项目 | 实测 | 是否在关键路径 |
| --- | ---: | :---: |
| `GET /api/v1/agents` | 18ms | 否 |
| `GET /api/v1/runtime/status` | 7–13ms | 否 |
| securityd `agents.list` | 24.6ms | 否 |
| 端侧 `POST /v1/runs` | 182–219ms | 是，包含在 bibit Hook 内 |
| 同时段 `systemd reload` | 1201ms | 环境干扰，不计入 |

Console 的控制面接口本身不是瓶颈。用户感知的慢主要来自 Worker 侧的准入扫描和 Hook 子进程。

---

## 8. 优化建议

按预计收益排序。

### 8.1 Hook 常驻或解释器复用（收益最大）

**问题**：每次 Hook 都重新启动 Python 并 import 重依赖，固定成本 300–500ms。

**方案**：

- 为每个插件维护一个常驻 Hook worker 进程，通过 stdin/stdout 或 UDS 接收多次调用；
- 或者使用预热过的解释器池，预加载 `bianbu_agent_sdk.memory`、security client 等模块；
- 保留现有的 seccomp、`NoNewPrivileges`、securityd 身份与进程组语义，不能为了复用而绕开隔离边界。

**预计收益**：

- `before_run` 关键路径从约 706ms 降到 300–400ms；
- `after_run` 从约 819ms 降到 300–400ms；
- 单轮可省 **300–700ms**，具体取决于常驻进程能否复用 import 后的模块状态。

**风险**：常驻进程需要处理插件崩溃、内存增长、Hook 超时和取消；安全边界必须与当前一次性进程等价。

### 8.2 能力快照按 revision/mtime 缓存

**问题**：每个 Run 准入都全量遍历并解析插件、MCP、技能和 Agent 包，中位数 251ms。

**方案**：

- 用目录 `mtime`、inode 或轻量 watcher 维护 revision；
- revision 未变时复用上一次已验证的 `InventorySnapshot`；
- 仍然保留「显式 reload」和「发现越界文件」的强制全量路径；
- 不能只依赖缓存而漏掉 out-of-band 安装，至少要用廉价 stat 指纹兜底。

**预计收益**：准入空档减少 **200ms 左右**，38 个 Run 的中位数可从 0.419s 降到 0.2s 附近。

### 8.3 合并技能快照与全量快照

**问题**：`reload_installed` 先调用 `skill_snapshot_provider` 扫描技能，再调用全量 `_snapshot`，后者又包含技能；随后两份都做 `verify_snapshot`。重复扫描约 72.6ms，重复校验约 7.4ms。

**方案**：全量快照已经包含技能时，从全量快照派生技能子集，只校验一次；只有在 `skills_only` 场景才使用独立的技能快照。

**预计收益**：**约 70–80ms/次**，实现风险低，适合优先落地。

### 8.4 端侧 intent 的 HTTP 往返

**问题**：`bibit-intent.before_run` 的 638.6ms 中有 271.4ms 是 `POST /v1/runs`；独立测量为 182–219ms。

这部分是实际执行应用启动的端侧服务时间，不应简单归为「Runtime 开销」。可优化的方向包括：

- 把 `launch_app` 的确认与响应拆开，先返回可发布的回复，再异步完成窗口拉起；
- 或者在端侧服务内复用已加载的分类器/规则，减少每次请求的准备时间。

**预计收益**：最多 **100–200ms**，但会改变「回复时应用是否已经可见」的语义，需要产品确认。

### 8.5 轨迹标签与准入事件

**问题**：`Startup` 这个名字误导了排障方向，且准入阶段没有细粒度事件。

**方案**：

- 前端把首个空档改为 `Admission`，或根据是否存在 `starting_agent` 区分 `Startup` 与 `Admission`；
- 后端发布 `admission.snapshot.started/finished`、`admission.lease.acquired` 等事件；
- 在 Hook 结果中保留 `duration_ms`，但不要把它当作行宽；轨迹行宽仍应以墙钟跨度为口径。

**预计收益**：不直接降低延迟，但能让后续性能问题在轨迹里直接定位，避免把准入扫描误判为进程启动。

---

## 9. 复现方法

### 9.1 拉取会话事件

```bash
curl -s \
  "http://127.0.0.1:9899/api/v1/sessions/session_db8ff41b-88ab-4a91-8db9-9be8c26b6007/events" \
  | jq '.data[] | {seq, time, type, run_id, data}'
```

重点看：

- `user/message.time` 到第一个 `hook/invoked.time` 的差；
- 每个 `hook/result.data.duration_ms`；
- `run/completed.time` 与 `user/message.time` 的差。

### 9.2 统计 38 个 Run 的准入空档

`/tmp/gaps.py` 的做法是：对每个会话的每个 `user/message`，找下一条同 `run_id` 的事件，计算时间差，最后输出中位数、最小值和最大值。

```bash
python3 /tmp/gaps.py
```

本次结果：

```text
median 0.419s  min 0.004s  max 16.920s  n 38
```

`max=16.920s` 是一次长尾异常，来自 `session_b535d42e-d5fd-4a05-a085-6d289fe4c4a6`，不代表常态。排除它之后，其余样本集中在 0.005–1.366s，主体在 0.38–0.51s。

### 9.3 Hook 子进程基准

在 K3 上用 Runtime 自带解释器执行：

```bash
/usr/lib/bianbu-agent-runtime/venv/bin/python -c pass
/usr/lib/bianbu-agent-runtime/venv/bin/python -c 'import bianbu_agent_sdk.memory'
```

再用 Hook 的真实 `command` 和 payload 走一遍 `sh -c` + stdin，得到端到端数据。

---

## 10. 附录：Hook 耗时汇总

| Hook 点 | Hook | `duration_ms` | 是否关键路径 |
| --- | --- | ---: | :---: |
| `before_run` | `bianbu.memory/memory.before_run` | 724.8 | 是 |
| `before_run` | `bianbu.security/security.before_run` | 500.5 | 否 |
| `before_run` | `bianbu.bibit.intent/bibit-intent.before_run` | 638.6 | 否 |
| `after_run` | `bianbu.memory/memory.after_run` | 817.0 | 是 |
| `after_run` | `bianbu.security/security.after_run` | 621.9 | 否 |

关键代码位置：

- `bianbu_agent/runs/manager.py:713`：`before_run` Hook 调用。
- `bianbu_agent/runs/manager.py:722`：`direct_reply` 短路。
- `bianbu_agent/runs/manager.py:564`：`after_run` Hook 调用。
- `bianbu_agent/plugins/host.py:104`：单个 Hook 的计时与事件发布。
- `bianbu_agent/hooks/command.py:126`：Hook 子进程启动。
- `bianbu_agent/security/runtime_process/worker.py:444`：Run 准入前的能力重扫。
- `bianbu_agent/plugins/manager.py:474`：`reload_installed` 的实现。
- `bianbu_agent/console/web/static/js/trajectory.js:156`：`Startup` 标签的判定。

---

## 11. 一句话总结

「打开终端」的 1.954s 不是模型慢，也不是 Worker 重启；它是 **0.43s 的 Run 准入扫描 + 0.71s 的 `before_run` 子进程 + 0.82s 的 `after_run` 子进程**。先把 Hook 子进程常驻化、再把能力快照做成 revision 缓存，是收益最高、风险最可控的两个方向。
