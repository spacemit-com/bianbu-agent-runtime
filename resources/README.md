# 系统预装资源

此目录只保存随 Runtime 发布的资源定义，整棵树 1:1 映射到
`/usr/share/bianbu-agent-runtime/`，包升级可以覆盖。本文件只描述源码布局，不随包安装。

- `default-profile/`：出厂 profile。`config.yaml` 是系统默认配置；`starter-agent.yaml`
  是默认 Agent 模板，构建时由 `debian/render-starter-agent.py` 渲染，把部署模型的占位值
  换成实际选择。
- `models/`：模型与服务商默认定义。
- `plugins/`：插件清单、事件 Hook 配置、外部脚本和插件内资源。
- `skills/`：预装技能及其辅助文件。
- `mcps/`、`agents/`：独立预装的 MCP 与 Agent 定义，按需增加；即使为空，包里也保留目录。

每个用户的稳定系统 Agent 实例由 `scripts/provision-system-agent.py` 发布，不属于本目录。
内置工具实现位于 `bianbu_agent/tools/`。

用户通过 AgentHub 安装或应用编辑的资源、配置、会话和工作文件保存在用户数据目录。
同 ID 的用户资源优先读取；编辑预装定义产生用户覆盖，不修改系统文件。

开发环境使用 `config/development.yaml`，`run.sh` 将 `BIANBU_SYSTEM_RESOURCE_ROOT`
设为此目录。运行时数据目录由配置里的 `data_dir` 决定，开发默认是项目内 `.bianbu-agent/`，
仅保存本地运行数据，不纳入 Git。
