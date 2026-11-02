# Bianbu Agent Runtime 性能测试报告

> 代码基线：`main` `8e734b49`（`bianbu/0.1.217`），securityd `bianbu/0.1.53`，
> bibit-intent `0.1.95`
> 测量设备：SpacemiT K3（`riscv-123`），以 `10.0.91.235` 为主，`10.0.91.146` 作对照
> Runtime：`127.0.0.1:9899`，system service，运行身份 `bianbu`

本报告只保留当前实测数据。每次在 K3 上重新测量后，直接用新数字覆盖对应章节，
不保留历史版本或历史日期。

本报告测量两条真实链路：

1. **云端普通问答**：「你好，请用一句话介绍你自己」，由 `bibit-intent` 判定为 `open_question` 后交给 `deepseek-flash`；
2. **端侧直答**：「打开终端」，由 `bibit-intent` 规则命中并直接发布 `direct_reply`，不调用云端模型。

同时记录本轮删除 network learning 后的运行态验证结果。

---

## 1. 结论

| 路径 | 首字延迟 p50 | 首字延迟 p90 | 完整 Run p50 | 结论 |
| --- | ---: | ---: | ---: | --- |
| 云端普通问答 | **1.987s** | 5.391s | 2.206s | 本地固定开销约 0.6s，其余是云端模型首字；尾部受上游排队影响明显 |
| 端侧直答「打开终端」 | **0.744s** | 0.789s | **0.772s** | 本地路径已进入亚秒级，准入空档仍是最大固定成本 |
| 切换安全配置（yolo ↔ safe） | — | — | **7.28–7.35s** | 两个方向都重建 Worker；用户要等完整冷启动 |
| 新建专家后的第一条消息 | **12.07–12.93s** | — | **12.67–13.61s** | 创建 API 约 1.1–1.4s，首次使用才触发约 7.7–8.9s 的 Worker 冷启动 |

最重要的五点：

1. **端侧直答的首字 p50 为 0.744s，完整 Run p50 为 0.772s**。相比 `docs/performance-guide.md` 在 0.1.178 上记录的完整 Run 1.954s，本次完整链路下降约 60%；但版本、暖机状态和环境不同，不能当作严格的 A/B 对比。
2. **每条请求从 `user/message` 到第一个 Hook 启动仍有约 0.52–0.66s 的空档**（p50 0.530s）。这段发生在 Hook 之外，属于 Run 准入、Worker RPC、调度和事件落库的固定成本，是下一轮优化最值得动的部分。
3. **Hook 本身已不再是端侧直答的瓶颈**。除 `bibit-intent.before_run` 的规则推理 p50 约 206ms 外，其余 Hook 的 p50 都在 18–29ms；云端路径的所有 Hook 都在 27ms 以内。
4. **专家首次启动仍是最大的可感知停顿**。切换安全配置和新专家第一条消息都会触发同一个 Worker 冷启动；停旧 Worker 加 securityd 写策略只有 0.1–0.2s，时间几乎全部花在新 Worker 的进程启动、插件/MCP 初始化和握手。串行 MCP 时代约 18.7s，并发生效后约 9.9s，最新的 `0.1.217` + `bibit-intent 0.1.95` 实测为 **7.3–8.2s**。
5. **冷启动里最大的单项仍是 4 个 MCP 子进程**。并发生效后，4 个 MCP 的总 wall time 从串行的 10.9–11.2s 降到约 4.2s；`bibit-intent 0.1.95` 又把这个数字压到 **2.5–2.7s**（单个 bibit MCP 的 `initialize` 从 2.7–3.7s 降到 1.6–2.1s）。`list_tools` 始终只有 0.01–0.03s，瓶颈完全在子进程启动和模块初始化。

---

## 2. 环境与版本

当前对比两块板，均为 SpacemiT K3、8 核 riscv64、Bianbu 4.0.8：

| 项目 | `10.0.91.235` | `10.0.91.146` |
| --- | --- | --- |
| 内核 | `6.18.3-generic #1.0.10.4+20261022010305` | `6.18.3-generic #1.0.9.4+20261011010255` |
| Runtime | **`0.1.217`** | `0.1.215` |
| securityd | **`0.1.53`** | `0.1.52` |
| bibit-intent | **`0.1.95`** | `0.1.94` |
| AgentMemory | `0.1.31` | `0.1.31` |
| 服务状态 | `active (running)`，`0.0.0.0:9899` | `active (running)`，`0.0.0.0:9899` |

`0.1.217` 与 `0.1.53` 是在 `10.0.91.235` 上用 `dpkg-buildpackage -us -uc -b`
原生构建后安装的；`10.0.91.146` 保持 archive 上的 `0.1.215` / `0.1.52`，
作为 `bibit-intent 0.1.94` 的对照。

两块板的版本号都取自板上 `dpkg -l` 的实际安装包。`10.0.91.235` 的包在该板上用
`dpkg-buildpackage -us -uc -b` 原生构建；`10.0.91.146` 保留 archive 版本，用于
单独观察 `bibit-intent 0.1.95` 的启动优化，避免把 Runtime 改动和 MCP 改动混在一起。

---

## 3. 测量口径

使用 `scripts/ttft-probe.py`：

- `click_s`：从 `POST /sessions/{id}/prompt` 发出，到会话日志里第一个 `text-chunks.time0`；
- `ttft_s`：从 `user/message` 事件，到第一个 `text-chunks.time0`；
- `gap_ms`：从 `user/message`，到最早一条 `hook/invoked`；
- `api_ms`：`POST /sessions/{id}/prompt` 的 HTTP 往返；
- 完整 Run：从 `user/message` 到 `run/completed`。

采样方式：

- 云端问答、端侧直答各先跑 1 轮预热，预热样本不计入统计；
- 正式样本各 10 个独立会话、每个会话 1 轮；
- 另各跑 3 个保留会话的详细样本，用于统计全部 Hook 和完整 Run 时间；
- 同一 Hook 点的多个 Hook 并发执行，因此关键路径取最慢 Hook，不能把各 Hook 耗时相加。

专家首次启动另用 `scripts/expert-start-probe.py`：

- 创建一个使用 `bianbu.bibit.intent`、`bianbu.memory`、`bianbu.security` 的新专家；
- 记录 `POST /agents` 创建耗时、`POST /sessions` 耗时和 `POST /prompt` 返回耗时；
- 再记录 `user/message` → 第一个 Hook、首个 token 和 `run/completed`；
- 共创建 3 个新专家各测一次；另建 1 个新专家，连续开两个会话对比冷/暖差异。

冷启动分段（§5.3）在板端用独立计时 harness 完成：

- 分别计时 Python 导入、`load_runtime_config()`、`create_runtime()` 和
  `PluginManager.start()`；
- 在 `PluginHost._start_candidate()` 前后打点，得到每个插件的启动耗时；
- MCP 部分用 MCP SDK 直接做 stdio 握手，分别记录进程 spawn、`initialize()`
  和两次 `list_tools()`；
- §5.5 的“切换中发消息”用后台 `security-mode.update.v1` 请求加
  `scripts/ttft-probe.py` 复现。

---

## 4. 首字与完整 Run

### 4.1 云端普通问答（n=10）

| 指标 | p50 | p90 | 均值 | 最小 | 最大 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `click_s` | 2.081s | 5.483s | 2.901s | 1.967s | 6.601s |
| `ttft_s` | **1.987s** | 5.391s | 2.770s | 1.875s | 6.510s |
| `gap_ms` | 529.9ms | 539.1ms | 531.1ms | 516.2ms | 559.2ms |
| `api_ms` | 99.4ms | 135.8ms | 136.8ms | 93.5ms | 443.1ms |

详细样本（n=3）的完整 Run：`2.048s / 2.206s / 2.235s`，p50 **2.206s**。

10 个样本中有 8 个首字落在 1.875–2.156s；两个异常样本为 5.391s 和 6.510s。
这两个样本的 `gap_ms`（528.7ms / 559.2ms）和本地 Hook 耗时都正常，因此尾部延迟来自
云端模型排队或网络，不是 Runtime 本地抖动。

### 4.2 端侧直答「打开终端」（n=10）

| 指标 | p50 | p90 | 均值 | 最小 | 最大 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `click_s` | 0.840s | 1.476s | 0.979s | 0.795s | 1.548s |
| `ttft_s` | **0.744s** | 0.789s | 0.756s | 0.707s | 0.891s |
| `gap_ms` | 540.5ms | 570.6ms | 553.9ms | 514.1ms | 663.2ms |
| `api_ms` | 99.9ms | 705.6ms | 229.1ms | 91.9ms | 804.8ms |

详细样本（n=3）的完整 Run：`0.757s / 0.772s / 0.779s`，p50 **0.772s**。

`click_s` 的尾部来自两次 705.6ms / 804.8ms 的 prompt HTTP 往返；首字本身的 p90 只有
0.789s，说明抖动发生在请求入队阶段，而不是 Hook 或直答逻辑。

---

## 5. 专家首次启动与安全配置切换

### 5.1 切换安全配置

在 `com.bianbu.627d97ad908c` 上通过
`agents.security-mode.update.v1` 来回切换 `yolo` / `safe`。API 只有在新的 Worker
就绪后才返回，因此下面就是从用户点击到可以继续对话的完整等待时间。

| 方向 | n | p50 | 最小 | 最大 |
| --- | ---: | ---: | ---: | ---: |
| `safe → yolo` | 3 | **18.692s** | 18.389s | 20.157s |
| `yolo → safe` | 3 | **18.792s** | 18.516s | 20.034s |

一次带阶段拆分的采样：

| 阶段 | `yolo → safe` | `safe → yolo` |
| --- | ---: | ---: |
| 停旧 Worker + 写入 securityd 新策略 | 0.157s | 0.109s |
| 启动新 Worker 到 ready | **18.676s** | **18.241s** |
| 合计 | 18.833s | 18.350s |

结论：**安全模式切换的耗时几乎全是 Worker 冷启动，securityd 更新本身不是瓶颈**。
切换必须重建 Worker，因为 host 进程和 sandbox 的网络命名空间、挂载表和 seccomp
都在启动时固定。两个方向都一样慢，说明主机进程和 bwrap 沙箱的 Worker 初始化成本相近。

systemd scope 日志显示，一个被替换的 Worker 在退出前 `Consumed 18.526s CPU time over
18.465s wall clock time`，即启动期间基本占满一个核。日志里还能看到多个 MCP 服务器连续
收到 `ListToolsRequest`，说明插件/MCP 工具发现是这段 CPU 开销的重要组成。

### 5.2 新建专家后的第一条消息

新专家通过 `POST /agents` 创建时不启动 Worker；第一条消息才触发冷启动。3 个新专家：

| 指标 | p50 | 最小 | 最大 |
| --- | ---: | ---: | ---: |
| `POST /agents` 创建 | 0.914s | 0.876s | 1.003s |
| `POST /sessions` | 23.0ms | 19.7ms | 25.6ms |
| `POST /prompt` 返回 | 37.6ms | 35.9ms | 41.4ms |
| `user/message → 第一个 Hook` | **18.952s** | 18.668s | 19.057s |
| 首字 | **19.435s** | 19.134s | 19.523s |
| 完整 Run | **19.860s** | 19.680s | 20.023s |

冷启动时 `before_run` 的三个 Hook 也会变慢：`bibit-intent` 约 469–483ms，
`memory` 约 415–469ms，`security` 约 345–408ms；`after_run` 约 336–431ms。
这与暖机后的 18–29ms 形成明显对比。

同一个新专家的第二个会话：

| 指标 | 第一会话（冷） | 第二会话（暖） | 变化 |
| --- | ---: | ---: | ---: |
| `gap_ms` | 18,707.3ms | 396.0ms | 降至约 1/47 |
| 首字 | 19.157s | **0.614s** | 降至约 1/31 |
| `click_s` | 19.188s | 0.675s | 降至约 1/28 |

因此，新建专家本身并不慢（约 0.9s），慢的是第一条消息触发的首次 Worker 初始化。

### 5.3 冷启动的耗时构成

上面约 18–19s 的冷启动并不是单一阶段，而是 Worker 进程自身的 Python
导入、插件 Generation 构建、4 个 MCP 子进程初始化依次串行叠加的结果。
对同一个 host Worker 的启动分段测量（设备负载约 6）：

| 阶段 | 耗时 | 说明 |
| --- | ---: | --- |
| Python 解释器 + Runtime 模块导入 | **4.4s**（4.3–5.4s） | `bianbu_agent.server.runtime` 累计 4.2s，其中 `engines.default`（LangChain / LangChain-OpenAI）2.2s |
| 插件 Generation 构建 | **12.2s**（12.20–12.35s） | 共 7 个插件 |
| 其中普通插件 3 个 | 0.06s | `bibit.intent` 0.02s、`memory` 0.01s、`security` 0.03s |
| 其中 MCP 连接 4 个 | **10.9–11.2s** | 串行执行，明细见下表 |
| Agent materialize、securityd 注册、Worker 激活 | 约 1.5–2.0s | 由总时长反推 |
| 合计 | **约 18.3s** | 与 systemd scope 实测的 18.5s 一致 |

4 个 MCP Server 逐个直接拉起并握手的结果：

| MCP Server | 进程启动 + 导入 + 初始化 | 工具数 |
| --- | ---: | ---: |
| `bianbu.bibit.intent/bibit-readonly` | 3.12s | 45 |
| `bianbu.bibit.intent/bibit-control` | 3.20s | 102 |
| `bianbu.bibit.intent/bibit-admin` | 3.13s | 34 |
| `bianbu.memory/memory` | 1.65s | 4 |

单个 bibit MCP 的 3.1s 还可以继续拆开：

```text
0.80s   Python 导入 bibit_intent.mcp_server
0.39s   解析 tools.yaml
1.04s   load_skills() 解析技能 YAML
~0.9s   FastMCP 创建并注册全部工具
```

三个 bibit group 是三个独立的 `python -m bibit_intent.mcp_server` 进程，
每份进程都会把同一套 `tools.yaml` 和 skills 重新解析一遍，因此每份
约 1.4s 的解析成本被重复了三次。改动前 MCP 连接由
`PluginHost._build_generation()` 串行 `await`：每个 Server 在 `setup()` 里先
connect + `list_tools()`，随后
`health()` 又 `list_tools()` 一次。但实测 `initialize()` 占 3.1s，
两次 `list_tools()` 分别只有 0.01–0.02s，**瓶颈是子进程冷启动和模块
初始化，不是工具枚举**。0.1.211 已改为并发，见 §5.6。

### 5.4 为什么 4 个 MCP 没有并行启动

这不是依赖要求，而是改动前的实现选择。`PluginHost._build_generation()`
原来对 `self._candidates` 顺序执行 `await self._start_candidate(...)`
（`bianbu_agent/plugins/host.py:621`、`:646`）；`PluginRegistry.resolve()`
先做拓扑排序，因为普通插件可能把 service/event 发布给后续插件消费，
这个循环保留了“按顺序启动”的假设，没有按依赖层并发。

但 MCP candidate 是独立叶子：`bianbu_agent/mcp/sources.py:107` 构造的
`PluginManifest` 没有 `dependencies`，`setup()` 也只在自己的
`PluginScope` 上登记 `mcp_runtime`，不向 `generation_services` /
`generation_events` 发布共享对象。因此这 4 个 MCP 完全可以并发启动。

实测（同一台板，load average 约 6.4，直接对 4 个 MCP 做 stdio 握手）：

| 方式 | 4 个 MCP 总 wall time | 单个 Server |
| --- | ---: | --- |
| 串行（当前实现） | 10.9–11.2s | bibit 3.1–3.2s，memory 1.65s |
| 并发（`asyncio.gather`） | **4.03–4.16s** | bibit 3.47–3.67s，memory 2.11s |

并发后单个 Server 只慢了约 0.3–0.5s，但总时间从 11s 降到 4s，节省约
6.7s，相当于把冷启动从约 18.5s 压到约 12s。这说明板上仍有并行余量。

注意不能简单对全部 candidate 做 `gather`：普通插件之间可能有依赖，
共享的 `generation_services` / `generation_events` 也需要确定性的发布
顺序。安全做法是按拓扑层分批，只对同层且不写共享服务的 candidate 并发
（MCP 正好属于这一类），并保持 `started`、`statuses` 和 diagnostics 的
确定性顺序。

0.1.211 已按这个边界实现：普通插件继续按拓扑顺序串行，MCP 叶子使用
`asyncio.Semaphore(4)` 有界并发；如果 MCP 声明了依赖，或有普通插件依赖
MCP，则自动退回原拓扑串行路径。

### 5.5 实际路径：切换到 host 后发 `hi`

用户反馈的操作路径（sandbox → host，然后发 `hi`）在轨迹上显示约 16.7s。
把“切换尚未完成就发送消息”的场景直接复现：

```text
t0       POST /security-api/invoke  agents.security-mode.update.v1 (safe -> yolo)
t0+3s    POST /sessions/{id}/prompt  "hi"
t0+18.4s 模式切换 API 返回（新 Worker ready）
```

探针结果：`api_ms=66.7ms`、`gap_ms=15,477.3ms`、`ttft=18.856s`，
即 `hi` 的 HTTP 请求立刻被接受，但 Run 一直等到新 Worker 可执行才开始。
另一次历史轨迹（prompt 为默认问候语，路径相同）：

```text
14:41:26.140  host Worker scope 启动
14:41:34.434  user/message
14:41:45.030  第一个 Hook（等待 10.596s）
14:41:47.687  发起模型请求
14:41:48.964  第一个 token（模型首字 1.277s）
14:41:50.033  run/completed（整轮 15.599s）
```

会话日志是 15.60s，UI 显示 16.7s，差额约 1.1s 来自前端点击到请求落库。
这一轮的开销是：

| 阶段 | 耗时 |
| --- | ---: |
| 等待 Worker 冷启动完成 | **10.60s** |
| `before_run` 三个 Hook（并发，取最慢） | 1.99s |
| `after_context` + `before_model` | 0.69s |
| 云端模型首 token | 1.28s |
| `after_model` + `after_run` + 收尾 | 1.07s |

作为对照，Worker ready 之后再发同样的 `hi`：`gap_ms=539ms`、首字 3.264s、
整轮 4.672s。**因此 16.7s 不是 `hi` 本身或模型慢，而是这条消息落在了
新 host Worker 的冷启动窗口里。**

0.1.211 复测同样的“切换中发 hi”：`gap_ms=6,867.4ms`、首字 **10.483s**、
`click_s=10.54s`，分别比修复前下降约 56% 和 44%。

后续优化方向：

1. 把 `engines.default`（LangChain / LangChain-OpenAI）改为首次用到云端
   模型时再导入，有望去掉约 2s 的固定启动成本；
2. 进一步把三个 bibit group 合并为一个进程，去掉重复的 registry/skills
   解析；
3. 去掉 `setup()` 之后 `health()` 的第二次 `list_tools()`，或复用
   `setup()` 的结果；
4. 切换模式时先启动新 Worker 再停旧 Worker，或常驻一对 host/sandbox 热
   Worker（每个约 332MB），把 18s 的切换等待移出用户路径；
5. 前端在切换 API 返回前禁止发送，或把消息显式标记为“正在启动专家”，
   避免用户把冷启动时间误读为对话延迟。

### 5.6 0.1.211 并发启动验证

在 `10.0.91.235` 上构建并安装 `0.1.211`（riscv64），Runtime 以 `bianbu`
身份运行，测试时 load average 约 6。

安全模式切换（每次等待新 Worker ready 后再开始下一次）：

| 方向 | 样本（ms） | p50 |
| --- | --- | ---: |
| `safe → yolo` | 9637 / 9666 / 9716 | **9.67s** |
| `yolo → safe` | 9876 / 9922 / 9952 | **9.92s** |
| 同模式重复请求 | 50 | 50ms（不重建 Worker） |

对比修复前的 18.69s / 18.79s，双向都下降约 47–48%。

Worker 日志里，4 个 MCP 的 `ListToolsRequest` 从原来的相隔 3–3.5s 变成
在 **1.6s 内全部出现**；`/api/v1/mcp-servers` 返回：

| MCP Server | 工具数 | 状态 |
| --- | ---: | --- |
| `bianbu.bibit.intent/bibit-admin` | 34 | healthy |
| `bianbu.bibit.intent/bibit-control` | 102 | healthy |
| `bianbu.bibit.intent/bibit-readonly` | 45 | healthy |
| `bianbu.memory/memory` | 4 | healthy |

再用“列出当前工作空间里的文件，不要修改任何文件”走真实对话，日志出现
`before_tool` / `after_tool` Hook，`run/completed` 正常，说明并发后 MCP
工具不只是能列出，也能实际调用。

关于“MCP 完全后台启动”：技术上可以，但当前 Generation 的 ready 语义包含
工具目录。把 MCP 全部放到后台会让 Worker 提前 ready，却可能让第一批 Run
在工具尚未 publish 时执行；要保证正确性还需要额外的 run gate 或连接复用。
现有的 `preparing + reload` 路径适合 npm 安装这种本来就要等待的场景，
不适合直接套到已可启动的 MCP。本次先采用有界并发，保留首条消息的工具
可用性。

### 5.7 0.1.215 冷启动优化：导入链与工具目录

0.1.211 把 MCP 启动改成有界并发后，一次安全模式切换仍然是约 9.7s。本轮在
K3 上给 Worker 启动链路逐段打点（load average 5.7–7.3），得到一次 8.87s 样本：

| 阶段 | 耗时 |
| --- | ---: |
| systemd/bwrap 起进程 → Worker 模块导入 | 0.45s |
| Worker RPC socket ready | 0.03s |
| 导入 `bianbu_agent.server.factory` | **1.69s** |
| 打开 stores | 0.06s |
| 插件发现 + capability 目录构建 | **0.88s** |
| MCP Server 启动（4 个子进程并发） | **4.67s** |
| Generation 收尾 | 0.45s |
| Agent 物化 + securityd 注册 | 0.21s |
| `runtime.start` 剩余 | 0.39s |

其中 MCP 的 4.67s 里，`start`（spawn + `initialize` 握手）占 3.8–4.0s，
`list_tools` 只有 0.02s；也就是说耗时几乎全在子进程启动，不在工具枚举。

据此做了六项改动：

1. `langchain_openai` 改为首次构造模型时才导入。它顶层导入会连带拉起
   `openai.types` 的 responses/graders 全套，而 Worker 只有第一条消息才建模。
2. `langchain_core.language_models.chat_models`（连带 `langchain_core.tracers`
   → `langsmith`）与 `bianbu_agent.engines.langgraph` 延迟到建模、构图时导入。
3. 把 `MCPAuthorizer` 类型别名拆到 `bianbu_agent/mcp/authorization.py`，插件发现
   和 Plugin 管理器不再通过它触发 `mcp.client`（即 `mcp` SDK）导入。
4. `tools/runtime_services.py` 的 `BaseChatModel` / `BaseTool` 只用于注解，改为
   `TYPE_CHECKING`；`langgraph.types.interrupt` 改到调用点导入。此前它把上面
   已经延迟掉的 `chat_models`、`capture` 又拉了回来。
5. `ProviderCaptureStore` / `ProviderCaptureCallback` 改为首次使用时创建：诊断
   录制是 Run 级行为，不该出现在 Worker 启动路径上。
6. 内置工具目录构建（34 个 Tool 的模块导入 + schema 构造，0.5–1.0s）改为在
   MCP 子进程握手期间用 `asyncio.to_thread` 并行执行。Generation 仍然等工具
   目录和 MCP 都完成才发布，ready 语义不变。

导入耗时（K3，暖）：

| 指标 | 优化前 | 优化后 |
| --- | ---: | ---: |
| `import bianbu_agent.server.runtime` | 4.03 / 4.18 / 4.34s | 1.22 / 1.31 / 1.30s |
| `import bianbu_agent.server.factory`（Worker 实际路径） | 2.08 / 2.20 / 2.39s | 1.22 / 1.28 / 1.29s |

第二行的“优化前”是前四项改完、但 `tools/runtime_services.py` 尚未处理时的样本：
它当时把 `chat_models` 和 `capture` 重新拉回，正好解释了为什么单看
`server.runtime` 已经降到 1.3s，Worker 路径却仍要 2.2s。

端到端安全模式切换（每次等待新 Worker ready 再开始下一次）：

| 方向 | 优化前 p50 | 优化后样本（ms） | p50 |
| --- | ---: | --- | ---: |
| `safe → yolo` | 9.67s | 8139 / 8145 / 8137 | **8.14s** |
| `yolo → safe` | 9.92s | 8246 / 8454 | **8.35s** |

两个方向都下降约 1.3–1.6s（约 14–16%）。切换后 `/api/v1/mcp-servers` 仍是
6 个 server 全部 `healthy/ready`，工具数不变（34 / 102 / 45 / 4 / 9 / 1）。

上表是在手工同步的文件上测的；换成 archive 上的正式 `0.1.215` 包（`dpkg -i`
后重启服务，load average 5.74）复测为 `safe → yolo` **8.15s**、
`yolo → safe` **8.28s**，与上表一致。

端到端降幅小于导入省下的 2.8s，原因是：`create_runtime` 在后台线程执行，和
supervisor 的 Hello/attest 本来就有一部分重叠；导入变快后，前置段落的瓶颈变成
securityd 握手而不是 Python 导入。当前最大单项已经变成 MCP 子进程启动
（4.5–4.7s），它由 bibit-intent 的三个 `python -m bibit_intent.mcp_server` 进程
（仅 `BIBIT_MCP_GROUPS` 不同）各自完整导入一遍依赖造成；要再压缩需要把三个
group 合并到一个进程，属于 bibit-intent 的启动协议改动，不在 Runtime 侧。

字节码方面：Runtime 包内 3142 个 `.py` 全部带 `.pyc`（缺失 0），服务环境还设置了
`PYTHONDONTWRITEBYTECODE=1`，Worker 启动不存在运行期编译成本。

### 5.8 0.1.216 常驻 Hook 启动预热

0.1.215 的 Hook 已经是常驻模式，但进程仍要到第一次 Run 才启动。Worker 重启后
首轮和次轮的 Hook 差距因此非常明显：

| 样本 | 8 个 Hook 合计 |
| --- | ---: |
| Worker 重启后的首轮 | 4.88s（单个 328–1727ms） |
| 同一 Worker 的次轮 | 0.55s（单个 4–235ms） |

本轮把“第一次调用才启动”改成“Worker 启动时就启动”，同时把插件自己的懒导入也
提前到常驻进程空闲期执行：

1. `CommandProcessPool.prewarm()` 只 spawn 常驻进程，不发送业务请求；失败只记
   debug，不影响 Plugin 启动。
2. `HookContribution.prewarm` 由命令 Hook 加载器提供，`PluginHost` 在 MCP 子进程
   启动完成后再调度预热，避免和 MCP 抢 CPU；`before_run` 最先，工具类 Hook 最后，
   并发上限 3。
3. `protocol.main(handler, warmup=...)` 在常驻模式读取请求前执行插件预热：
   bibit-intent 预加载 `edge_http`，memory 预导入 SDK 的 context/lifecycle，
   security 预构造 securityd bridge。security bridge 预热失败时会清掉降级单例，
   避免 securityd 稍后恢复后该常驻进程仍永久 fail-open。
4. 生成或关闭 Generation 时仍由原 `CommandProcessPool.close()` 统一回收；预热
   进程与普通常驻进程共用同一套 idle 600s 回收规则。

在 `10.0.91.146` 上用 `0.1.215` 加本轮补丁做 A/B（load average 6.8–8.3，同一
Worker 内连发两次“你好”）：

| 指标 | 关闭预热 | 开启预热（7 个样本 p50） |
| --- | ---: | ---: |
| 首轮 8 个 Hook 合计 | **3228.5ms** | **604.9ms** |
| 次轮 8 个 Hook 合计 | 360.0ms | 291.3ms |
| 首轮与次轮差值 | **2868.5ms** | **313.6ms** |

也就是首轮 Hook 成本下降约 **81%**，首轮与次轮的 Hook 差值下降约 **89%**。
开启预热后，模式切换返回时 14 个常驻 Hook 进程（bibit 4、memory 2、security 8）
已在 1s 内全部出现，单个 RSS 约 18–27MiB，合计约 0.3GiB。

模式切换本身没有可测的额外延迟（load average 8.28，同一时段 A/B）：

| 安全模式切换 | 样本（ms） | p50 |
| --- | --- | ---: |
| 关闭预热 | 9144 / 9227 / 9295 / 9469 / 9608 / 9682 | **9.38s** |
| 开启预热 | 8811 / 9057 / 9301 / 9691 / 10239 / 10741 | **9.50s** |

两者差异在噪声内；此前 8.15s 的基线是在 load average 5.74 下测的，不能和本轮
load average 8.28 的样本直接比较。

首轮完整 Run 仍比次轮慢约 2.1s，但已经不在 Hook 上。事件时间线显示首轮
`before_run` 结束后到 `after_context` 之间有一段约 2.3s 的空白，次轮只有约
0.2s；这段是首次构建 LangChain/LangGraph 引擎（延迟导入 provider SDK、构造工具
和 graph），随后 provider 首 token 也比次轮多约 0.4s。下一步应把引擎导入链或
已解析 Agent 的 engine 构造做成 Worker 启动后的后台预热，而不是继续加 Hook 进程。

### 5.9 bibit-intent 0.1.95 启动优化复测

同事反馈 `bibit-intent` 优化了 MCP 启动时间。本轮用两块板做对照：`10.0.91.235`
装 `Runtime 0.1.217` + `bibit-intent 0.1.95`，`10.0.91.146` 装
`Runtime 0.1.215` + `bibit-intent 0.1.94`。两边都用 MCP SDK 的 stdio 客户端直接
握手（不经过 Runtime），每项跑 2 轮，避免把 Runtime 的调度开销算进 MCP 自身。

4 个 MCP Server（3 个 bibit group + 1 个 memory）的启动时间：

| 板 / 版本 | 串行 4 个 | 并发 4 个 |
| --- | ---: | ---: |
| `10.0.91.146` / bibit `0.1.94` | 12.34s / 12.26s | 4.16s / 3.76s |
| `10.0.91.235` / bibit `0.1.95` | **8.89s / 8.29s** | **2.54s / 2.70s** |

单进程 `initialize`：

| Server | bibit `0.1.94` | bibit `0.1.95` |
| --- | ---: | ---: |
| bibit-admin / control / readonly | 3.0–3.7s | **1.6–2.1s** |
| memory | 1.6–2.0s | 1.6–1.8s |

`list_tools` 两边都只有 0.01–0.03s，工具数也一致（34 / 102 / 44–45 / 4），
所以这次优化动的是子进程启动和模块导入，不是工具枚举。结果是：**并发启动总时间
下降约 35–39%，单个 bibit MCP 进程的启动成本下降约 40%**。Runtime 侧不需要改动，
MCP 并发启动的收益直接体现在总时长上。

端到端也同步变快。同一套安全模式切换探针（每次等新 Worker ready 再发下一次）：

| 板 / 版本 | `safe → yolo` | `yolo → safe` | 同模式重复 |
| --- | ---: | ---: | ---: |
| `10.0.91.146` / `0.1.215` | 9.89s | 8.70s | 62.8 / 24.4ms |
| `10.0.91.235` / `0.1.217` | **7.35s** | **7.28s** | 68.9 / 72.1ms |

新建专家后的第一条消息（每板 3 个样本）：

| 板 / 版本 | `gap_ms` | `ttft` | `completed` | `create_ms` |
| --- | ---: | ---: | ---: | ---: |
| `10.0.91.146` / `0.1.215` | 9358.8 / 9039.1 / 10749.4 | 13.650 / 13.117 / 14.927s | 15.334 / 13.905 / 15.770s | 864 / 1025 / 1010 |
| `10.0.91.235` / `0.1.217` | **7734.7 / 8201.2 / 8902.8** | **12.067 / 12.727 / 12.933s** | **12.670 / 13.476 / 13.612s** | 1433 / 1069 / 1064 |

`completed - ttft` 只有 0.7–1.7s，是云端模型生成时间；剩下的都是本地准备。
专家首启的本地空档从 9.0–10.7s 降到 7.7–8.9s，约省 1.3–1.8s，与 MCP 并发启动
省下的时间量级一致。`create_ms` 反而略高（1.4s），说明创建接口本身不是瓶颈，
用户感知的等待全在创建之后的新 Worker 冷启动。

`10.0.91.235` 一侧的 Worker 日志里，第一轮 `before_run` 的三个 Hook 分别为
`intent 415.7ms / memory 276.1ms / security 61.8ms`，第二轮降到
`intent 47.2ms / memory 207.5ms / security 53.9ms`，说明 0.1.216 的常驻 Hook
预热在 bibit 0.1.95 上仍然有效，首轮多出的约 0.4s 只在 Hook 内部。

对“MCP 再合并成一个进程”这条优化方向的影响：`0.1.95` 已经把单个 bibit 进程的
启动从 3.0–3.7s 降到 1.6–2.1s，合并三个 group 的理论收益从原来的 2–3s 缩小到
约 1–1.5s，而且要付出协议改动和工具隔离的成本。短期更划算的是继续压缩
Worker 侧 `import bianbu_agent.server.factory`（约 1.2–1.3s）以及 MCP 握手之后
的 Generation 收尾。

---

## 6. 时间分解

### 6.1 端侧直答

以详细样本 p50 拆账：

```text
0.540s   user/message -> 第一个 Hook 启动（接纳、Worker RPC、调度、落库）
0.206s   bibit-intent.before_run 规则推理（最慢 Hook，决定 before_run 窗口）
0.744s   首字
0.772s   完整 Run（after_run 与终态落库）
```

`bibit-intent.before_run` 与 `memory/security` Hook 并发，因此本地关键路径约为
`0.540 + 0.206 ≈ 0.746s`，与实测首字 0.744s 基本闭合。

### 6.2 云端问答

```text
0.530s   user/message -> 第一个 Hook 启动
0.027s   最慢的 before_run Hook
~1.43s   模型请求、上游排队与首 token
1.987s   首字
2.206s   完整 Run
```

本地固定开销约 0.56–0.60s；云端路径的方差主要来自模型上游，而不是 Hook。

---

## 7. Hook 耗时

### 端侧直答（3 个详细样本）

| Hook | p50 | 最小 | 最大 |
| --- | ---: | ---: | ---: |
| `bianbu.bibit.intent / before_run` | 205.6ms | 173.8ms | 207.7ms |
| `bianbu.memory / after_run` | 28.7ms | 24.1ms | 29.8ms |
| `bianbu.memory / before_run` | 24.9ms | 24.1ms | 26.7ms |
| `bianbu.security / before_run` | 18.5ms | 16.3ms | 20.8ms |
| `bianbu.security / after_run` | 18.1ms | 17.2ms | 18.2ms |

### 云端问答（3 个详细样本）

| Hook | p50 | 最小 | 最大 |
| --- | ---: | ---: | ---: |
| `bianbu.bibit.intent / before_run` | 26.9ms | 24.6ms | 27.7ms |
| `bianbu.memory / after_run` | 26.3ms | 24.8ms | 31.6ms |
| `bianbu.memory / before_run` | 25.9ms | 21.3ms | 26.6ms |
| `bianbu.security / before_model` | 18.0ms | 15.2ms | 18.5ms |
| `bianbu.security / before_run` | 16.3ms | 15.2ms | 16.8ms |
| `bianbu.security / after_context` | 13.5ms | 12.4ms | 22.6ms |
| `bianbu.security / after_run` | 10.9ms | 10.6ms | 11.4ms |
| `bianbu.security / after_model` | 2.6ms | 2.6ms | 2.7ms |

---

## 8. 与旧基线对比

`docs/performance-guide.md` 在 0.1.178 上记录的「打开终端」样本：

- 首字：1.135s；
- 完整 Run：1.954s。

本次同一路径：

- 首字 p50：0.744s，下降约 **34%**；
- 完整 Run p50：0.772s，下降约 **60%**。

差异同时来自版本演进、Worker/Hook 暖机、预编译字节码和当前环境，不能直接归因于
删除 network learning；但可以确认本轮改动没有让端侧直答路径退化。

---

## 9. Network learning 删除验证

在 `10.0.91.235` 上安装最终构建包后逐项检查：

| 检查项 | 结果 |
| --- | --- |
| 包内容中的 `network-learning` 文件 | 无 |
| `bianbu-agent-network-learning.service` | `Unit ... could not be found` |
| `network_learning` / `network-learning` 进程 | 无 |
| `/run/bianbu/network-learning` | 已由设备启动准备脚本清理 |
| 网络策略模式 | 仅 `deny_all`、`whitelist`、`allow_all` |
| 产品审计视图 | 历史 `network_learning_*` 行被过滤，不再显示为「其他安全事件」 |
| Runtime 健康 | `AGENT_READY`，Worker `running` |

删除后不再有常驻的 root collector 进程和独立 systemd unit；Runtime 服务本身
cgroup 内存约 144MB。该数据是当前整机运行态，不用于宣称精确的节省值。

---

## 10. 环境噪声与限制

- 测试时板子并非空闲：`bibit-audio` 约占 60% CPU；同时运行 GLM-Edge、ASR 和两个
  Embedding `llama-server`，合计占用约数 GB 内存；`spacemit-ai-gateway`、
  `openclaw-gateway` 等后台进程也在运行；load average 约 6。
- 同一时段 `bianbu-agentforce.service` 因缺少 `/usr/lib/hermes-agent/venv/bin/python3`
  每约 5 秒重启一次，`bianbu-agent-runtime-register.service` 也在周期触发；这会给
  systemd 和 CPU 带来额外噪声。首次启动的 18.5s CPU 时间来自 Worker 自身 scope，
  但整机负载仍可能放大绝对耗时。
- 云端样本受上游模型排队影响，p90 明显高于 p50；端侧直答的 p90 更稳定。
- 两块板不是严格的 A/B：`10.0.91.235` 是板上原生构建、内核 `6.18.3 #1.0.10.4`，
  `10.0.91.146` 是 archive 包、内核 `6.18.3 #1.0.9.4`。MCP 启动时间是绕过 Runtime
  直接用 MCP SDK 握手的，基本只反映 bibit-intent 版本差异；端到端数字则同时包含
  两块板的环境差异，按量级参考、不当作精确归因。
- 本报告是单机、单次时段的黑盒采样，适合定位瓶颈和确认无回归，不用于承诺 SLA。
- `scripts/ttft-probe.py` 已适配当前会话日志的紧凑 `text-chunks` 记录，并修复空响应
  清理失败；这些修复属于测量工具，不改变 Runtime 行为。

---

## 11. 原始样本

### 云端问答（`ttft_s` / `click_s` / `gap_ms` / `api_ms`）

```text
5.391 / 5.483 / 528.7 / 97.9
1.996 / 2.085 / 539.1 / 95.3
1.969 / 2.057 / 531.1 / 93.5
6.510 / 6.601 / 559.2 / 98.6
1.992 / 2.087 / 533.6 / 103.5
1.981 / 2.077 / 523.5 / 102.4
2.156 / 2.593 / 534.1 / 443.1
1.954 / 2.046 / 516.2 / 100.1
1.879 / 2.009 / 523.5 / 135.8
1.875 / 1.967 / 521.5 / 97.9
```

### 端侧直答（`ttft_s` / `click_s` / `gap_ms` / `api_ms`）

```text
0.751 / 0.849 / 544.5 / 103.7
0.712 / 0.800 / 522.5 / 93.4
0.736 / 0.830 / 536.4 / 99.2
0.789 / 0.883 / 566.3 / 100.5
0.777 / 1.476 / 570.6 / 705.6
0.716 / 0.803 / 527.2 / 93.0
0.707 / 0.795 / 514.1 / 95.1
0.891 / 0.978 / 663.2 / 91.9
0.751 / 1.548 / 562.9 / 804.8
0.732 / 0.828 / 531.7 / 103.5
```

### 安全模式切换（总耗时，毫秒）

```text
10.0.91.146 (Runtime 0.1.215 / bibit-intent 0.1.94)
safe -> yolo: 9892.0 / 8702.3
同模式重复: 62.8 / 24.4

10.0.91.235 (Runtime 0.1.217 / bibit-intent 0.1.95)
safe -> yolo: 7353.6
yolo -> safe: 7280.3
同模式重复: 68.9 / 72.1
```

### 新建专家首次使用

`create_ms / session_ms / gap_ms / ttft_s / completed_s`

```text
10.0.91.146 (Runtime 0.1.215 / bibit-intent 0.1.94)
864.2 / 21.9 / 9358.8 / 13.650 / 15.334
1024.8 / 118.3 / 9039.1 / 13.117 / 13.905
1010.1 / 101.1 / 10749.4 / 14.927 / 15.770

10.0.91.235 (Runtime 0.1.217 / bibit-intent 0.1.95)
1432.9 / 23.9 / 7734.7 / 12.067 / 12.670
1069.0 / 23.1 / 8201.2 / 12.727 / 13.476
1064.1 / 28.4 / 8902.8 / 12.933 / 13.612
```

同一新专家第二个会话（暖）：

```text
gap_ms=396.0  ttft_s=0.614  click_s=0.675
```

---

## 16. 自然 DLP 替身

### 16.1 测量目的

验证「模型拿不到真实 PII，也看不出发生了替换；用户看到的仍是真实值」这条链路，
并记录它在会话内的开销与稳定性。

- 设备/路径同前：K3 `10.0.91.235`，`http://127.0.0.1:9899`
- securityd `0.1.53`（含姓名边界与会话前缀复用修复），Runtime `0.1.217`
- 安全模式：`safe`

### 16.2 测量方法

1. 新建一个只挂 `bianbu.security` 插件、模型指向本地**录制型 provider** 的 Agent；
   该 provider 只是 OpenAI 兼容的 HTTP 服务，会把收到的每个请求体原样落盘。
   这样「模型实际收到什么」是可证的，而不是靠轨迹推断。
2. 会话内连续三轮：

   ```text
   请记住：我叫林嘉禾，我父亲叫周若彤，手机号 13812345678，邮箱 alice@example.com。只回复 OK。
   请完整复述第一轮我告诉你的两个姓名和手机号。
   我父亲的名字是周若彤还是别人？只回答姓名。
   ```

3. 对三次 provider 请求体做字符串检索，统计四个真实值出现次数；
   同时读取会话事件流中的 `assistant/text.delta` 与 `run/completed`，确认用户侧是真实值。

> 方法上的一个坑：新建 Agent 默认是 `yolo`，此时 Hook 会返回
> `decision=ALLOW, mode=yolo, dlp_scanned=false`，DLP 根本不跑。
> 必须先把该 Agent 的 security mode 改成 `safe` 再测，否则会把「DLP 未启用」
> 误读成「DLP 泄漏」。首轮误测就出现过 12 次真实值出现，全部由这个原因造成。

### 16.3 结果

以下数字来自最近一次「K3 原生构建 + 安装 + 抓包」的完整复测：

| 指标 | 结果 |
| --- | --- |
| provider 请求数 | 3 |
| 请求体中真实值出现次数 | **0**（`RESULT: PASS`） |
| 模型看到的姓名 | 三轮均为 `丁敏秀`（本人）、`彭然若`（父亲） |
| 模型看到的手机号 | 三轮均为 `13879455773` |
| 模型看到的邮箱 | 三轮均为 `user43590064@example.com` |
| 会话映射条数 | 4，三轮不增长 |
| 用户侧终态 | `OK` / 含真实姓名与手机号的复述 / `周若彤` |

三轮请求体中，真实值与替身的出现情况：

```text
req1  real=[]   stand-in: 丁敏秀 / 彭然若 / 13879455773 / user43590064@example.com
req2  real=[]   stand-in: 同上
req3  real=[]   stand-in: 同上（历史消息与当前请求一致）
```

第三轮尤其关键：用户自己写的是真实值 `周若彤`，模型上下文里仍然是
`彭然若`，且模型回答 `周若彤` 后经 Runtime 还原，用户看到真实值。

### 16.4 当轮 Hook 与 Run 耗时

最新一次在 K3 上原生构建并安装 `bianbu-agent-securityd 0.1.53` /
`bianbu-agent-runtime 0.1.217` 后的实测（3 轮样本）：

| Hook | 平均 | 最大 |
| --- | ---: | ---: |
| `security.before_run` | 20.12ms | 21.98ms |
| `security.after_context` | 31.23ms | 42.66ms |
| `security.before_model` | 33.06ms | 42.76ms |
| `security.after_model` | 3.06ms | 3.28ms |
| `security.after_run` | 14.01ms | 17.05ms |

DLP 相关 Hook 的固定开销约 60–70ms/Run。三轮完整 Run：

```text
7.42s（新 Worker 冷启动）/ 0.80s / 0.84s
```

首轮包含 Worker 冷启动，后两轮是同会话的稳态值；稳态下安全链路约占 0.8s 的 9%。
这一轮没有引入新的进程或额外 IPC：替身生成与映射都在既有 Hook 进程的会话表里完成。

### 16.5 本轮修掉的三个缺陷

复测过程中发现并修复的问题，均已在板上验证：

1. **会话 key 不一致**：`current_session_id()` 优先取 `session_id`/`thread_id`，
   而 Hook 映射按 `RunControl.session_id` 存。上下文清理会旋转 `thread_id`，
   于是映射被灌到错误的 key 下。改为优先取 `RunControl.session_id`。
2. **`merge()` 只有 `ref -> value` 单向索引**：后续扫描真实值时查不到已有替身，
   走确定性生成器发现该替身已被占用，碰撞重试出 counter=1 的第二套值
   （`丁敏秀` 变 `顾秀禾`、`13879455773` 变 `13890062838`）。
   现在 `get_or_create` 增加了会话内的真实值反查。
3. **姓名检测多吃一个字**：`周若彤还是别人` 捕成 `周若彤还`，这是「另一个真实值」，
   于是同一个父亲在第三轮拿到第三套替身 `曾思艳`。
   修复两层：姓名尾部虚词表补入 `还/都/又/却/仍/呢/吗/吧`；
   `_session_mask` 增加「检测值以已知真实值为前缀时复用并拼接后缀」的兜底。

回归用例：

- securityd `tests/unit/test_pii_scanner.py::test_trailing_adverb_after_a_name_is_not_part_of_the_name`
- securityd `tests/unit/test_dlp_handlers.py::test_over_captured_value_extends_the_existing_stand_in`
- runtime `tests/test_model_dlp_boundary.py::test_real_value_reuses_the_adopted_hook_stand_in`
- runtime `tests/test_model_dlp_boundary.py::test_session_scope_is_the_product_session_not_the_rotated_thread`

### 16.6 复现方式

K3 原生构建与安装（不使用 CI 产物）：

```sh
cd ~/bianbu-agent-security && dpkg-buildpackage -us -uc -b -j8
cd ~/bianbu-agent-runtime  && dpkg-buildpackage -us -uc -b -j8
dpkg -i ~/bianbu-agent-securityd_0.1.53_riscv64.deb \
        ~/bianbu-agent-runtime_0.1.217_riscv64.deb
systemctl restart bianbu-securityd bianbu-agent-runtime
```

端到端抓包（两个脚本都在 `scripts/acceptance/` 下）：

```sh
python3 scripts/acceptance/dlp_capture_provider.py 19900 &
python3 scripts/acceptance/dlp_pii_capture.py <provider-id> [agent-id]
```

脚本会把每个 provider 请求体落盘到 `/tmp/provider_requests.jsonl`，
并直接判定 `real=[]` 是否为全部请求成立；任何一个请求出现真实值都会返回非零。

### 16.7 尚未覆盖

- 本次只验证了姓名/手机/邮箱；身份证、银行卡、地址的替身格式由
  `securityd.dlp.pii_scanner.pseudonym` 保证（身份证校验位、银行卡 Luhn），
  但未做端到端 provider 抓包验证。
- 工具参数的还原链路（模型把替身写进工具参数、Runtime 还原后交给本地工具）
  在单测层覆盖，端到端只观察到 memory 工具的审批与写入，未做「替身进参数、
  真实值落盘」的抓包断言。
- 映射只在进程内存中，Worker 重启后同一会话的旧替身不可还原；
  这是既有的会话级设计边界。
