# 内置插件

每个插件的 `plugin.yaml` 只写身份（`schema_version`/`name`/`version`/`description`）；提供什么能力由包内目录决定，清单里不写贡献，也没有权限字段。
Hook 只使用外部命令协议：

```text
bianbu.<name>/
├── plugin.yaml
├── hooks/
│   └── <hook-id>/hook.json    # 事件、命令、超时和优先级
├── scripts/
│   ├── before_run.py         # 直接读取请求字典的业务处理器
│   └── protocol.py           # stdin/stdout JSON、SIGTERM 和拒绝结果
├── lib/                      # 可选：插件自己的共享客户端
├── tools/                    # 可选：插件提供的工具
├── skills/                   # 可选：技能
└── mcps/                     # 可选：MCP 配置
```

Runtime 从 `hook.json` 启动命令，将版本 1 JSON 请求写入 stdin。脚本在
stdout 输出一个 JSON 对象；日志写入 stderr。脚本通过 `PLUGIN_ROOT`、
`PLUGIN_DATA_ROOT` 和 `PLUGIN_WORKSPACE_ROOT` 获取路径，不导入 Runtime
的 Hook SDK，也没有 `hook.py` 或 Python 工厂入口。

随包只保留本仓自己维护的插件。`spacemit-power`、`superpowers` 这类第三方 Power
不再默认内置（用户约定 2026-09-29）：需要时由用户在 AgentHub/APM 上按需安装到
`~/.bianbu-agent/plugins/`，安装后的优先级见下。

- `bianbu.audit`：工具、完成和错误审计。
- 同名插件的优先级：`~/.bianbu-agent/plugins/<id>/`（用户在本机装的，例如从
  AgentHub 下载的第三方插件）排在随包目录之前。两边同名时只有用户那份生效，
  随包那份被覆盖，不会在目录里并列出现；用户那份删掉后随包那份自动回来。
- `bianbu.memory`：`mcps/memory/` 提供 AgentMemory MCP Server（检索、写入、
  删除、会话检索）；`memory.before_run` 检索、`memory.after_run` 写入，
  运行开始标记在脚本退出前完成。
- `bianbu.bibit.intent`：通过 HTTP 请求端侧服务；取消时请求服务停止执行。
- `bianbu.security`：八个安全事件。DLP 占位符映射通过私有 `state` 在同一
  会话的命令间传递，由 Runtime 保存在内存，不能放入公开 `metadata`。
  运行完成或终止错误时清空；可恢复的工具错误保留映射。
