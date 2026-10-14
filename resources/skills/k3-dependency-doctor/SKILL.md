---
schema_version: 2
name: k3-dependency-doctor
name_zh: K3 依赖医生
name_en: K3 Dependency Doctor
description: Find missing dependencies in a developer's project on Bianbu riscv64 (Python imports, C headers/so, pkg-config, npm), show a sourced and risk-labelled install plan, install only what the user confirms, then verify against the code. Use when a project fails to run with ModuleNotFoundError, ImportError, "cannot open shared object file", missing *.h/headers, or when the user says 缺依赖 / 装依赖 / 安装依赖 / 跑不起来 / 依赖检查.
description_zh: 在 Bianbu riscv64 上找出开发者项目真正缺失的依赖（Python import、C 头文件/so、pkg-config、npm），给出标注来源与风险的安装计划，只安装用户确认的条目，最后回到代码层验证。当项目报 ModuleNotFoundError、ImportError、cannot open shared object file、缺少 .h/头文件，或用户说缺依赖 / 装依赖 / 安装依赖 / 跑不起来 / 依赖检查时使用。
version: 0.1.1
---

# K3 Dependency Doctor

面向 SpacemiT K3 / Bianbu `riscv64` 的**开发者依赖管理**：从代码里找出真正缺的依赖，
给出带来源与风险的安装计划，用户确认后只装确认过的条目，最后回到代码层验证。

配套工具来自 bibit-intent MCP（只读的 `dep_scan` / `dep_status` / `dep_plan` / `dep_verify`，
需审批的 `dep_apply`）。软件源可信度分级、制品校验与获批后的边界遵循
`riscv-software-delivery`；本 Skill 只负责「代码缺什么 → 装什么 → 验证」这条链路。

## 铁律

1. **先探测，再安装。** 不许凭报错猜包名：先 `dep_scan` + `dep_status`，或用 `dep_plan` 一次完成。
2. **没有 `plan_id` 不许装。** `dep_apply` 只接受 `dep_plan` 生成的 `plan_id` 与编号；
   计划过期或项目文件已改动时它会拒绝执行，此时重新生成计划，不要绕过。
3. **安装前必须让用户看到依赖信息并确认**：装什么包、什么版本、从哪个源、多大、
   为什么要装（证据到 `文件:行`）、风险等级、是否需要 sudo。未确认不得调用 `dep_apply`。
4. **只装用户确认的编号。** 用户说「装 1,3」就只装 1 和 3，不要顺手把其它项一起装。
5. **装完必须验证。** 用 `dep_verify` 复验并报告证据（`import` 成功 / `pkg-config --exists` /
   头文件存在 / 命令在 PATH），不能只回报「命令成功」。
6. **绝不 `sudo pip`。** Python 依赖只装进项目 venv 或用户共享 venv；只有 apt 走 sudo。
7. **不自动装非索引来源。** `git+`、`http(s):`、`file:`、`-e`、本地路径依赖一律列进
   「需人工确认」，不自动安装，也不要替用户改写成 PyPI 包名。
8. **不自动添加 APT 源。** 需要第三方源时，把它作为一条**单独、显式**的步骤展示给用户，
   用户明确同意后才执行，并记录来源与签名信息；不得作为失败后的静默兜底。
9. **不改动无关状态。** 不改全局 PATH、不装 x86/arm64 制品、不 `curl | sh`、不静默 `make install`。
10. **源码构建要先补工具。** 计划里出现「需要源码构建」时，把「缺哪个构建工具、要 `sudo apt-get install`
    哪个包、编译耗时与失败风险」一起讲清楚；`dep_apply` 会先补计划里列出的工具再编译——不要自己
    用 `terminal` 去补工具，也不要跳过用户确认。
11. **源不可达时照实说。** 卡片标明「源不可达 / 能否离线装」时，按卡片讲：哪个源不可达、本地缓存
    里有什么、离线命令是什么。不自动换源、不静默改用缓存安装、不把「源里没有本机可用的 wheel」
    说成「断网」。

## 工作流

### 1. 收集项目事实

先确认项目目录（绝对路径）。用户没说清时问一次，不要拿 `/opt/bibit-intent` 之类的工具自身目录当项目。
然后调用：

- `dep_plan(path=<项目目录>)`：一次完成扫描 + 本机满足度 + 干跑计划，返回 `plan_id` 与依赖卡片；
- 需要单独排查时用 `dep_scan`（只列代码里的依赖）或 `dep_status`（只看缺哪些）。

写文件类工具（`write_file` / `patch`）之后，工具结果里可能被追加一行
`依赖提醒【未安装】：…（pip）；仅提示，未安装。`——这是 Runtime Hook 自动调的只读提醒。
如实转述给用户并问要不要出计划即可，**不要**因为看到提醒就去安装。

### 2. 展示依赖信息（必须）

把 `dep_plan` 的卡片原样讲给用户，至少包含每项的：编号、类型、名称、目标包与版本、
渠道（apt/pip）、真实来源、体积、风险、证据（`文件:行`）。缺失项多于 5 个时按渠道分组，
并明确说明「还有哪些项需要人工处理、为什么」。不要压缩成一句「要装 N 个包，可以吗」。

卡片里的「构建：…」与「离线降级：…」两行要一并转述：
- 「构建：」说明源码包要用哪些工具编译、当前缺哪个、确认后会先 `sudo apt-get install -y <工具>`；
- 「离线降级：」说明哪个源不可达、哪些项能用本地缓存离线装（手动步骤里给了 `--no-index` /
  `--no-download` 命令），哪些项没有缓存、必须等网络恢复。

### 3. 等待确认

只有用户明确同意（例如「装 1,3」或「全部安装」）后才继续。用户拒绝或要改范围时，
重新 `dep_plan` 再展示，不要自行缩减范围。

### 4. 执行

调用 `dep_apply(plan_id=<计划号>, items="1,3")`。执行是逐项进行的：失败即停，
已完成的项会带验证结果返回。需要源码构建的项会先补构建工具再编译，结果里会写
「（一并补齐构建工具：…）」。Runtime 会弹出审批，审批被拒绝时不要重试同样的调用。

### 5. 验证与收尾

- `dep_verify(plan_id=...)` 复核全量结果；验证证据按真实 import 名展示
  （例如 `requirements.txt` 里的 `pyahocorasick` 实际 import 的是 `ahocorasick`）；
- 回复时列出：装了什么、验证证据、仍需人工处理的项（含建议命令）、以及需要用户重启的进程/服务；
- 用户的项目代码在计划生成后被改动过时，要重新走一遍流程。

## 平台注意事项（K3 / Bianbu）

- **riscv64 + Python 3.14（cp314）**：优先用有 riscv64 wheel 的包。计划里出现
  「源码包，需要编译」意味着要本地编译：必需工具是 `gcc`/`make`/`pkg-config`/`python3-dev`
  （`Python.h`），`dep_plan` 会探好并在卡片里标出缺哪个、缺的用哪个 apt 包补；向用户说明
  编译耗时与失败风险，`dep_apply` 会先补工具再编译。
- **断网 / 源不可达**：SpacemiT 镜像只托管一部分包（未托管的 302 到 `pypi.org`），镜像上
  「只有其它 Python/平台版本的 wheel」时 pip 会报 `No matching distribution found`——这**不是**断网。
  按卡片的两种口径分别回复：源不可达（给离线命令或等网络）、源里没有本机可用产物（换源或手工构建）。
- **PEP 668**：系统 Python 不允许直接装包，必须走 venv；共享 venv 位于
  `~/.local/share/bibit-intent/pyvenv`，项目里有 `.venv` 时优先用项目的。
- **C 库/头文件**：运行时缺 `.so` 装 `libxxx1`，编译缺头文件装 `libxxx-dev`；
  apt 计划里出现「会移除其它包」时必须转人工，不要带 `--no-remove` 之外的花样。
- **本机没有 node/npm/rust**：Node 依赖与需要 rustc 的源码包会标记为不支持，
  如实告诉用户需要先装对应工具链，不要尝试变通。
- **无 swap、磁盘有限**：计划里体积异常大的项要在确认前提醒。

## 与其它能力的关系

- `riscv-software-delivery`：源可信度分级、第三方制品校验、获批后的执行边界；
  需要第三方源或 vendor 制品时遵循它，本 Skill 不重复定义。
- `vibe-coder`：产出的是无运行时依赖的离线 HTML 应用；一旦要引入 Python/原生依赖，
  交回本 Skill 处理。
- bibit-intent 包工具（`pkg_install` / `pip_install` / `install_local`）：用户直接说
  「安装 xxx」这种单包请求仍走它们；只有「检查/补齐项目依赖」才走 `dep_*` 链路。

## 降级

bibit-intent MCP 不可用（工具缺失或调用失败）时，只做只读诊断：
读取项目文件说明缺少什么、需要哪个包，并给出建议命令让用户自行执行；
不要用 `terminal` 里的 `pip install` / `apt-get install` 绕过审批链路。
