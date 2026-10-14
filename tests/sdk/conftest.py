"""SDK 单元测试与宿主机正在运行的生产 securityd 隔离。"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_external_security_daemon(monkeypatch):
    """包管理单测使用临时无签名 fixture，不应受机器级 enforce 策略污染。

    代码签名的 allow/deny 与真实 securityd 链路由安全子系统专项测试覆盖；这里验证
    SDK 自身的原子安装、冲突、隔离与回滚语义。若直接连接 worker 上的生产
    securityd，这些无签名 fixture 会被预期拒绝，导致测试结果依赖执行机器状态。
    """
    monkeypatch.setenv("BIANBU_SECURITY", "0")
