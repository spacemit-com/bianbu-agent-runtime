# K3 单次提交任务验收

这两项是真实模型、真实工具的显式验收入口：使用 riscv-porter 移植
fast-pack/MaskedVByte，以及生成中国象棋网页并实际打开、走棋和重置。
日常测试不连接 K3、不创建专家或提交消息；只有显式执行下面的流程才会触达真机。

## 执行

在候选 Runtime/Security 已激活的 K3 上，以 `bianbu` 用户进入 Runtime 源码，
按下面的 `plan/prepare/submit/watch` 分步执行。服务使用本机 9899，实际项目树位于
`/home/bianbu/.bianbu-agent/workspace/acceptance-随机值/`。测试必须在 K3 执行以读取
真实产物，不能仅把远程 API 代理到本机后把模型回复当成成功。它会产生真实模型费用，
每项新建独立专家与会话、设置该新专家 YOLO，继承当前 Bibit 的模型和能力；
不修改原 Bibit 的模式/配额或其他会话。项目提示禁止修改其他项目与服务。
Host 模式仍共享文件系统，目录隔离不等于安全沙箱。

人工分步执行使用 `k3_single_submission.py` 的 `plan/prepare/submit/watch`：
默认 plan 仅输出提示，prepare 和 submit 必须带 `--execute`。例如：

```sh
python3 scripts/acceptance/k3_single_submission.py plan maskedvbyte --state /tmp/example.json
```

真实状态与事件保存在 `~/.bianbu-agent/acceptance-evidence/<时间-case-随机值>/`：
`state.json`、`state.events.jsonl`、三个阶段日志及验证通过后的 `validation.json`。
项目保留源码、构建输出、日志、截图、`acceptance-evidence.json`，不会自动清理。
不要把含真实工具输出的证据目录加入 Git。

## 一次提交与失败

每个用例恰好一次初始 prompt，不使用 goal/continue，不自动批准审批。
提交前落盘 `prompt_attempted`，网络中断也不自动重复 POST。已有 state 禁止重建，
再次 submit 被拒绝；可对原状态重复 watch，事件按 seq 去重并补齐分页。
观察最多一小时，超时测试失败但不取消运行；不得通过继续提示把失败验收改称成功。
重新执行 pytest 会创建另一项独立验收，不能覆盖或冒充原来一次提交的结果。

## 检查范围与证据边界

两个真实用例均要求：一条用户消息、同一 run 成功终止、无审批干预、成功工具调用、
存在的项目内证据文件（可列出多个路径，每项至少一个非空结果）。`run/completed` 或模型口头成功单独不能通过。

MaskedVByte 还要求：构建、官方测试、codec smoke、riscv64 ELF、sanitizer 五项证据，
中文移植报告，以及校验 ELF64 头及加载段范围，确认存在 RISC-V 可执行产物（e_machine=243）。
基线已通过可不修改源码；sanitizer 不适用须在证据中写明依据。

象棋还要求实际 HTML、打开/红走/黑走/重置四项证据及可完整解码的 PNG/JPEG 截图。
浏览器执行记录可以是成功 browser_navigate 和至少三次成功 browser_click，也可以
是终端成功执行项目内的 CDP/Playwright 控制脚本，脚本须包含导航、点击和截图调用。
只有 curl、生成文件、打印脚本名均不通过。脚本执行记录仅证明验证程序被调用；
仍须人工对照实际日志、画面和状态变化，不能凭代码含有浏览器 API 名称宣称成功。

`validation.json` 明确标记 `semantic_review_required: true`：这些自动断言是
**证据完整性门槛**，不能单凭通用网页的点击次数证明点击的是正确棋子，也不能
独立证明全部棋规或每条编译日志的含义。最终业务验收还须审阅保存的工具参数/结果、
截图、源码和测试日志；不得把此文件称为自动证明象棋全部规则正确。

