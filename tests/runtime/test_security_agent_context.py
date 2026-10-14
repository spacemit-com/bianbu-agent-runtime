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

    # 安全权限设置：可读 / 可写都是「包含 + 排除」两段式，网络单独一段；
    # 自定义补充项已删除。
    assert "permissions.get.v1" in js
    assert "permissions.set-group.v1" in js
    assert "security-permission-read" in js
    assert "security-permission-read-exclude" in js
    assert "security-permission-write" in js
    assert "security-permission-write-exclude" in js
    assert "security-permission-network" in js
    assert "security-permission-custom" not in js
    assert "security-permission-grid" in css

    # 系统调用黑名单没有强制基线：新装设备默认不限制。
    assert "强制基线" not in js
    assert "mandatory" not in js

    # DLP 动作是三态 + mask；不再有风险列。
    assert "'allow', 'ask', 'deny', 'mask'" in js
    assert "security-dlp-action" in js
    assert ".security-dlp-action" in css
    assert ".security-dlp-textarea" in css

    # YOLO 模式保留，且启用前要求确认。
    assert "security-capability-mode-toggle" in js
    assert "启用 YOLO 模式" in js
    assert "security-mode-card__status" in css
    assert "security-mode-card__risk" in css

    # 旧的 Token 配额、网络策略与配置中心面板已删除。
    assert "token-budget.update.v1" not in js
    assert "token-budget.get.v1" not in js
    assert "security-token-limit" not in js
    assert "security-config-grid" not in js
    assert "syncSecurityNetworkMode" not in js
    assert "network-learning.events.v1" not in js


def test_security_center_agents_tab_keeps_inventory_and_suspend():
    html = (ROOT / "bianbu_agent/console/web/chat.html").read_text()
    js = (ROOT / "bianbu_agent/console/web/static/js/console.js").read_text()

    assert "已注册 Agent" in js
    assert "agents.list.v1" in js
    assert "agents.suspend.v1" in js
    assert "暂停请求已发送" in js
    assert "security-agent-inspect" in js
    assert "audit.query.v1" in js
    # 「策略判定」调试面板已移除：判定结果由工具策略列直接表达。
    assert "判定策略" not in js
    assert "permission.check.v1" not in js
