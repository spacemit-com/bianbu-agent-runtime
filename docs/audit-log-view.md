# 审计日志的查看方式

审计日志回答的是“谁代表谁、想做什么、谁同意了、结果如何”，不是聊天记录。
原始记录仍然逐条保存在 securityd 的审计链里；控制台通过 `audit.view.v1`
把每条记录翻译成一句人话，并补上类别、结果和风险。

## 六个类别

| 类别 | 一句话 | 典型事件 | 记什么 |
|---|---|---|---|
| `identity` | 谁接入了 | `bind_caller`、`caller_identity` | Agent、调用者、UID/PID、绑定时间 |
| `decision` | 想做什么 | `check_permission`、`hook:*`、工具名、`uds_dlp_scan` | 动作、对象、范围、决策、策略版本 |
| `confirmation` | 征求过你的同意 | `ask`、`confirm`、`session_trust` | 请求的能力、批准/拒绝、会话范围 |
| `change` | 设置被改了 | 策略、模式、网络、系统调用、注册/注销 | 旧值、新值、生效对象 |
| `anomaly` | 出过什么问题 | `build_sandbox_policy`、`build_seccomp_profile`、`runtime.*`、未知事件 | 失败原因、进程/Worker、策略版本 |

`admin:*` 管理员操作仍写入 securityd 的底层审计链，但不再出现在产品审计列表里。

`bind_caller` 和 `caller_identity` 不同：前者是“某个调用者第一次和 Agent 建立
绑定”，后者是“调用者声称的身份与绑定不符，被拒绝”。前者是接入事实，后者是
身份冒用告警。

## 默认只看需要关注的事

控制台默认打开“只看需要我关注的”，保留拒绝、待确认、设置变更和异常；
常规放行仍然入链，但默认不铺在首屏。需要完整过程时关掉开关即可。

例行的身份刷新（`register_agent`）和在正常模式（YOLO）下跳过检查只记为
“正常”，因为它们由当前模式直接决定、每次调用都会出现；真正需要关注的是
模式切换、策略修改、拒绝、待确认和异常。这样“需要你关注”不会被模式本身的
副作用淹没。

同一 5 秒窗口内的事件会归成一组，只展开风险最高的一条，其余收在
“同一时间的另外 N 条”里。每条记录都可以展开“技术详情”，查看
`seq`、`op_type`、`target`、`result` 原始字段。

## 聊天内容不进入审计

提示词、模型回复（`content`）和思考内容（`think`）属于用户内容，不写入审计。
超过 1KB 的 Hook 载荷只记录指纹：

```json
{"kind":"content_fingerprint","bytes":4096,"sha256":"…","fields":["content","thinking"]}
```

这足以证明“这份内容被检查过”、支持事后关联同一载荷，同时不把原文留在
审计数据库里。

升级前已经写入 `uds_audit_batch` 的历史原文仍在审计链中，哈希链不能就地
改写；`audit.view.v1` 对这类记录统一显示为 `historical_payload_redacted`，
不再把原文返回给控制台。要彻底移除只能按审计保留策略轮转或归档旧分片。

## 接口

`POST /security-api/invoke`，`operation` 为 `audit.view.v1`。

请求参数：

| 参数 | 说明 |
|---|---|
| `agent_id` | 必填，Agent 标识 |
| `limit` | 1–200，默认 100 |
| `cursor` | 上一页返回的 `next_cursor`，首次为 0 |
| `snapshot_seq` | 第一页返回的高水位，后续页原样带回 |
| `from` / `to` | ISO 8601 时间范围，使用 `+00:00` 时区 |
| `attention` | `true` 时只返回需要关注的事件 |
| `category` | 六个类别之一 |
| `outcome_group` | `blocked` / `pending` / `approved` / `failed` |
| `filter` | securityd 原生过滤：`all` / `hook` / `deny` |

返回保留 `records`、`snapshot_seq`、`has_more`、`next_cursor`，并为每条记录增加：

```text
category / category_label      六类及中文标签
title                          一句话说明发生了什么
outcome / outcome_group        精确结果与展示分组
severity / severity_label      critical / warning / info
attention                     是否进入“需要关注”视图
object                         被操作对象（tool / caller / agent / worker…）
group_key                      5 秒分桶，用于界面归组
technical                      seq / op_type / target / result
```

`summary` 给出本页的 `total`、`attention`、`by_category`、`by_severity`
和实际扫描的 `scanned` 条数。类别和结果过滤在 Runtime 侧跨原始分页完成，
不会出现“先分页再过滤”导致的漏记录。
