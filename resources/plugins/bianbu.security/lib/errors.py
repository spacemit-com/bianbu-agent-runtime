"""安全拦截异常（INT-RT-02 / INT-RT-06）。

继承 PermissionError 的理由：bianbu_agent/plugins/host.py 的 _dispatch 对
BEFORE_RUN / BEFORE_TOOL / BEFORE_MODEL 三个干预点**专门重抛 PermissionError**，
其他异常一律 catch + warning + continue。这是项目既有的权限门惯例
（bianbu_agent/mcp/plugin.py 的 MCPAuthorizer 也用同一手法）。
"""
from __future__ import annotations

__all__ = ["SecurityDenied"]


class SecurityDenied(PermissionError):
    """securityd 判定拒绝。

    能力管理只有工具一个维度、策略三态 allow / ask / deny。deny 是明确拒绝，
    不能被 Agent 换个说法重试，所以一律终止整轮 run（should_terminate_run 恒为
    True）；ask 在到达这里之前已经由审批门处理。
    """

    def __init__(
        self,
        *,
        tool: str,
        capability: str,
        reason: str,
        decision: str = "DENY",
        degraded: bool = False,
    ) -> None:
        self.tool = tool
        self.capability = capability
        self.reason = reason
        self.decision = decision
        # degraded=True 表示这是 securityd 不可达时的 fail-closed 拦截，
        # 而非明确的权限拒绝 —— 审计与提示文案需要区分二者
        self.degraded = degraded
        super().__init__(f"[bianbu-security] {reason}")

    @property
    def should_terminate_run(self) -> bool:
        """deny 终止整轮，避免 Agent 反复试探被明确拒绝的工具。"""
        return True

    def to_metadata(self) -> dict:
        return {
            "tool": self.tool,
            "capability": self.capability,
            "decision": self.decision,
            "reason": self.reason,
            "degraded": self.degraded,
            "terminate_run": self.should_terminate_run,
        }
