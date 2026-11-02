from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_security_center_has_shared_agent_context_selector():
    html = (ROOT / "bianbu_agent/console/web/chat.html").read_text()
    js = (ROOT / "bianbu_agent/console/web/static/js/console.js").read_text()

    assert 'id="security-agent-context"' in html
    assert 'id="security-agent-select"' in html
    assert "securitySelectedAgent" in js
    assert "security-agent-select" in js
    assert "安全服务暂不可用" in js
    assert "暂无可用 Agent" in js
    css = (ROOT / "bianbu_agent/console/web/static/css/console.css").read_text()
    assert ".security-agent-context" in css
    assert ".dark .security-agent-context" in css
    assert ".security-agent-context select" in css


def test_security_center_keeps_six_pages_and_tool_policies():
    """安全页面六个页面：Agent / 能力管理 / 安全权限设置 / 系统调用黑名单 / DLP / 审计。"""
    html = (ROOT / "bianbu_agent/console/web/chat.html").read_text()
    js = (ROOT / "bianbu_agent/console/web/static/js/console.js").read_text()
    css = (ROOT / "bianbu_agent/console/web/static/css/console.css").read_text()

    for tab in ("agents", "capabilities", "permissions", "syscalls", "dlp", "audit"):
        assert f'data-security-tab="{tab}"' in html
    # 旧的配置中心 tab 不再存在。
    assert 'data-security-tab="configuration"' not in html
    # 系统调用黑名单与安全权限设置各自独立成页。
    assert 'id="security-panel-syscalls" class="hidden' in html
    assert 'id="security-panel-permissions" class="hidden' in html
    assert "syncSecuritySyscallPanel" in js
    assert "loadSecurityPermissionView" in js

    # 工具三态策略；旧的 risk / domain / scope / confirm 维度不再出现。
    assert "SECURITY_TOOL_POLICIES = ['allow', 'ask', 'deny']" in js
    assert "security-tool-policy" in js
    assert "security-cap-new" not in js
    assert "security-cap-scope" not in js
    assert "security-cap-modal-save" not in js
    assert "risk_level" not in js
    assert ".security-cap-grant" not in css
    assert ".security-cap-revoke" not in css

    # 安全权限设置：文件系统只有只读路径 / 读写路径两个原生列表，网络单独
    # 一段；嵌套排除与自定义补充项都已删除。
    assert "permissions.get.v1" in js
    assert "permissions.set-group.v1" in js
    # 生效范围预览：界面展示的是 securityd 编译出来的边界，不是另写的一份说明。
    assert "permissions.boundary.v1" in js
    assert "securityBoundaryPanel" in js
    assert "沙箱生效范围" in js
    # 边界展示宿主绝对路径，并在同一行给出沙箱内的对应路径。
    assert "专家数据根" not in js
    assert "security-boundary-path__sandbox" in js
    assert "路径映射：沙箱" in js
    # 映射只来自后端实际上报的 path_mapping；dsh 自带沙箱时不能凭空造一个。
    assert "view.path_mapping" in js
    assert "不由本框架的沙箱接管" in js
    assert ".security-boundary-grid" in css
    assert ".security-boundary-list" in css
    assert ".security-boundary-path__sandbox" in css
    # 边界只展示只读 / 读写两组原生挂载，排除项已经不在模型里。
    assert "security-boundary-grid--scopes" in js
    assert "可读但不可写" not in js
    # 每个框架的沙箱后端不同：设置页要展示本框架的实际范围，而不是把
    # Runtime 的挂载表套给 dsh；不可改的范围必须禁用保存。
    assert "settings.framework" in js
    assert "本框架实际只读" in js
    assert "本框架实际可写" in js
    assert "settings.editable" in js
    assert "该框架的文件范围由它自身的沙箱决定" in js
    assert "该框架的网络边界由它自身接管" in js
    assert "security-permission-read" in js
    assert "security-permission-write" in js
    assert "security-permission-read-exclude" not in js
    assert "security-permission-write-exclude" not in js
    assert "只读路径" in js
    assert "读写路径" in js
    assert "security-permission-network" in js
    assert "security-permission-network-mode" in js
    assert "SECURITY_NETWORK_MODES" in js
    assert "securityNetworkModeHelp" in js
    assert "security-permission-diff" in js
    assert "域名不能包含协议、路径或空格" in js
    assert "白名单模式至少填写一个域名" in js
    assert "填写域名后按白名单模式保存" not in js
    assert "security-permission-custom" not in js
    assert "security-permission-grid" in css
    assert ".security-network-modes" in css
    assert ".security-permission-diff" in css

    # 系统调用黑名单没有强制基线：新装设备默认不限制。
    assert "强制基线" not in js
    assert "mandatory" not in js

    # DLP 页只暴露两种选择：mask 与 allow（不脱敏）；不再有风险列。
    assert "const SECURITY_DLP_ACTIONS = ['mask', 'allow'];" in js
    assert "mask · 脱敏后放行" in js
    assert "allow · 不脱敏" in js
    assert "security-dlp-action" in js
    assert ".security-dlp-action" in css
    assert ".security-dlp-textarea" in css

    # 两种用户可见模式：安全模式（sandbox）与正常模式（YOLO/host）。
    # Agent 页可以切换模式；能力管理页只读，所有页面都说明两模式的行为差异。
    assert "security-capability-mode-toggle" not in js
    assert "changeSecurityAgentMode" in js
    assert "agents.security-mode.update.v1" in js
    assert "SECURITY_DEFAULT_AGENT_MODE = 'yolo'" in js
    assert "安全模式 · sandbox" in js
    assert "正常模式 · YOLO host" in js
    assert "SECURITY_MODE_BEHAVIOR" in js
    assert "securityModeBehaviorCard" in js
    assert "两种模式的行为差异" in js
    assert "本页可以切换，其它页面只读展示。" in js
    assert "本页只读展示，切换请到 Agent 页。" in js
    # 六个页面共用同一套页头与正文骨架，避免各页标题层级和间距不一致。
    assert "function securityPageHeader(" in js
    assert "function securityPage(" in js
    assert "security-page-header" in css
    assert "security-page-actions" in css
    assert "security-subpanel__heading" in css
    assert "security-page" in html
    for _view in ("agents", "capabilities", "permissions", "dlp", "audit"):
        assert f"securityPage('{_view}'" in js
    # 系统调用页是静态 HTML，模式说明由加载函数注入。
    assert "securityModeBehaviorCard('syscalls'" in js
    assert "security-mode-card__status" in css
    assert "security-mode-card__status--risk" in css
    assert ".security-mode-behavior__grid" in css

    # 旧的 Token 配额、网络策略与配置中心面板已删除。
    assert "token-budget.update.v1" not in js
    assert "token-budget.get.v1" not in js
    assert "security-token-limit" not in js
    assert "security-config-grid" not in js
    assert "syncSecurityNetworkMode" not in js


def test_security_center_agents_tab_keeps_inventory_and_unregister():
    html = (ROOT / "bianbu_agent/console/web/chat.html").read_text()
    js = (ROOT / "bianbu_agent/console/web/static/js/console.js").read_text()
    css = (ROOT / "bianbu_agent/console/web/static/css/console.css").read_text()

    assert "已注册 Agent" in js
    assert "agents.list.v1" in js
    assert "agents.unregister.v1" in js
    assert ">注销</button>" in js
    assert "暂停请求" not in js
    assert "security-agent-suspend" not in js
    assert "security-agent-unregister" in js
    assert "security-agent-inspect" in js
    # Agent 清单是唯一能切换模式的入口；能力管理页仍只读展示 badge。
    assert "security-agent-mode" in js
    assert "securityAgentModeControl" in js
    assert "securityAgentModeBadge" in js
    assert "agents.security-mode.update.v1" in js
    assert "<th>模式</th>" in js
    # 正常模式等于 YOLO/host，界面必须把失效项说清楚。
    assert "security-permission-warn" in js
    assert "security-permission-warn" in css
    # 审计页走产品化视图（一句人话 + 六类 + 技术详情），不再直接铺原始行。
    assert "audit.view.v1" in js
    assert "security-audit-tech" in js
    # 「策略判定」调试面板已移除：判定结果由工具策略列直接表达。
    assert "判定策略" not in js
    assert "permission.check.v1" not in js


def test_security_center_loads_agent_selector_before_rendering_tab():
    """「当前 Agent」是整页共用控件，进入安全页就要填充。

    之前只有「系统调用黑名单」页的加载函数会调用 agents.list.v1，默认的
    Agent 页进不去那条路径，下拉框就一直停在「加载中...」。
    """
    js = (ROOT / "bianbu_agent/console/web/static/js/console.js").read_text()

    assert "async function loadSecurityCenter(forceAgents = false)" in js
    assert "viewId === 'security') { syncSecuritySyscallPanel('agents'); loadSecurityCenter(); }" in js
    # 选择器加载必须独立于 syscall 页，且只发一次 agents.list.v1。
    assert "async function loadSecurityAgents(" in js
    assert js.count("securityInvoke('agents.list.v1'") == 1
    # 进入页面先取清单，再渲染 tab；syscall 页复用同一份结果。
    center = js.split("async function loadSecurityCenter(forceAgents = false)", 1)[1].split("async function loadSecurityOtherView", 1)[0]
    assert "await loadSecurityAgents(forceAgents);" in center
    assert center.index("await loadSecurityAgents(forceAgents);") < center.index("loadSecurityOtherView(tab")
    assert "loadSecuritySyscallView(false, loaded)" in center
