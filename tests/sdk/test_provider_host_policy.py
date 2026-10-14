"""D-8 Provider base_url 白名单（BN-SEC-CAPMAP-001 §6.6）。

Provider 的 base_url 决定**全部对话内容发往哪台服务器**，此前可注册任意
URL 且无任何校验 —— 一次 API 调用就能把所有会话导向攻击者的服务器，
而且不留痕迹：模型照样答，用户看不出区别。

比单个工具越权严重：工具越权影响一次操作，provider 改向影响全部会话，
泄露的是对话原文（往往含 DLP 想拦的内容）。
"""

from __future__ import annotations

import pytest

from bianbu_agent.models import ModelConfig, YAMLModelStore
from bianbu_agent.security.security_provider_policy import (
    ProviderHostRejected,
    allowed_provider_hosts,
    enforce_provider_host,
    provider_host_allowed,
)

_ENV = "BIANBU_ALLOWED_PROVIDER_HOSTS"


class TestHostParsing:
    def test_no_whitelist_allows_everything(self, monkeypatch):
        """未配置时不限制 —— 本地部署常连内网推理服务或 localhost，
        预置一份公有云白名单只会让人装不上自己的服务。"""
        monkeypatch.delenv(_ENV, raising=False)
        assert allowed_provider_hosts() == ()
        assert provider_host_allowed("https://anything.example/v1")[0] is True

    @pytest.mark.parametrize("url,host", [
        ("https://api.openai.com/v1", "api.openai.com"),
        ("https://API.OpenAI.COM/v1", "api.openai.com"),      # host 不区分大小写
        ("https://api.openai.com:8443/v1", "api.openai.com"),  # 端口不影响发给谁
        ("api.openai.com/v1", "api.openai.com"),               # 无 scheme 也要解析
        ("https://user:pw@evil.com/v1", "evil.com"),           # userinfo 不能混淆
        ("http://127.0.0.1:11434", "127.0.0.1"),
    ])
    def test_host_extraction(self, monkeypatch, url, host):
        monkeypatch.setenv(_ENV, "nothing-matches")
        assert provider_host_allowed(url)[1] == host

    def test_whitelist_separators(self, monkeypatch):
        monkeypatch.setenv(_ENV, " a.com , b.com ; c.com ")
        assert allowed_provider_hosts() == ("a.com", "b.com", "c.com")


class TestMatching:
    @pytest.fixture(autouse=True)
    def whitelist(self, monkeypatch):
        monkeypatch.setenv(_ENV, "api.openai.com,*.mycorp.internal,localhost")

    @pytest.mark.parametrize("url", [
        "https://api.openai.com/v1",
        "https://api.openai.com:8443/v1",
        "api.openai.com/v1",
        "https://llm.mycorp.internal/v1",
        "https://a.b.mycorp.internal/v1",       # 多级子域
        "http://localhost:11434/v1",
    ])
    def test_allowed(self, url):
        assert provider_host_allowed(url)[0] is True

    @pytest.mark.parametrize("url", [
        "https://evil.com/v1",
        "https://api.openai.com.evil.com/v1",   # 后缀欺骗
        "https://evilapi.openai.com/v1",        # 前缀拼接
        "https://mycorp.internal/v1",           # *.x 不匹配 x 本身
        "https://user:pw@evil.com/v1",
    ])
    def test_rejected(self, url):
        assert provider_host_allowed(url)[0] is False

    def test_unparseable_url_rejected(self):
        """配了白名单却解析不出 host —— 不能放过，否则畸形 URL 就是绕过手段。"""
        assert provider_host_allowed("not a url at all")[0] is False
        assert provider_host_allowed("https://")[0] is False

    def test_empty_base_url_not_checked(self):
        """空 base_url 不构成"数据发往外部"：记录可以先建后填，
        default_engine 只在 base_url 非空时才设置它。"""
        enforce_provider_host("")
        enforce_provider_host("   ")

    def test_enforce_raises_with_actionable_message(self):
        with pytest.raises(ProviderHostRejected) as ei:
            enforce_provider_host("https://evil.com/v1")
        msg = str(ei.value)
        assert "evil.com" in msg
        # 报错必须说清"该改什么" —— 否则用户只知道失败了
        assert _ENV in msg
        assert ei.value.host == "evil.com"
        assert "api.openai.com" in ei.value.allowed

    def test_is_permission_error(self):
        """继承 PermissionError：API 层与 CLI 已按它分流权限类错误。"""
        assert issubclass(ProviderHostRejected, PermissionError)


class TestStoreEnforcement:
    """校验必须挡在 store 的写事务里 —— 直接调 store 的调用方同样挡得住，
    而不是只在 HTTP 层拦（那样 CLI 与内部调用就绕过了）。"""

    @pytest.fixture
    async def store(self, tmp_path):
        s = YAMLModelStore(tmp_path / "models")
        await s.open()
        yield s
        await s.close()

    @pytest.mark.asyncio
    async def test_create_provider_rejected(self, store, monkeypatch):
        from bianbu_agent.models.providers import ProviderConfig

        monkeypatch.setenv(_ENV, "api.openai.com")
        with pytest.raises(ProviderHostRejected):
            await store.create_provider(ProviderConfig(
                id="evil", name="evil", protocol="openai-compatible",
                base_url="https://evil.com/v1", api_key="k"))

    @pytest.mark.asyncio
    async def test_create_provider_allowed(self, store, monkeypatch):
        from bianbu_agent.models.providers import ProviderConfig

        monkeypatch.setenv(_ENV, "api.openai.com")
        got = await store.create_provider(ProviderConfig(
            id="ok", name="ok", protocol="openai-compatible",
            base_url="https://api.openai.com/v1", api_key="k"))
        assert got.id == "ok"

    @pytest.mark.asyncio
    async def test_create_model_config_rejected(self, store, monkeypatch):
        monkeypatch.setenv(_ENV, "api.openai.com")
        with pytest.raises(ProviderHostRejected):
            await store.create(ModelConfig(
                "m1", "p1", "gpt", "https://evil.com/v1"))

    @pytest.mark.asyncio
    async def test_patch_provider_base_url_rejected(self, store, monkeypatch):
        """已存在的 provider 也不能被 PATCH 改到白名单外 —— 否则先合法注册
        再改向就绕过了门禁。"""
        from bianbu_agent.models.providers import ProviderConfig

        monkeypatch.setenv(_ENV, "api.openai.com")
        await store.create_provider(ProviderConfig(
            id="p", name="p", protocol="openai-compatible",
            base_url="https://api.openai.com/v1", api_key="k"))
        with pytest.raises(ProviderHostRejected):
            await store.patch_provider_resource(
                "provider-p", fields=frozenset({"base_url"}), label=None,
                base_url="https://evil.com/v1", api_key=None, parameters=None)

    @pytest.mark.asyncio
    async def test_no_whitelist_store_unrestricted(self, store, monkeypatch):
        """未配置白名单时 store 行为与改造前一致（不引入回归）。"""
        from bianbu_agent.models.providers import ProviderConfig

        monkeypatch.delenv(_ENV, raising=False)
        got = await store.create_provider(ProviderConfig(
            id="any", name="any", protocol="openai-compatible",
            base_url="https://whatever.example/v1", api_key="k"))
        assert got.base_url == "https://whatever.example/v1"


class TestApiErrorShape:
    def test_handler_registered_returns_403_not_500(self):
        """没有专门的处理器就落到 Exception 兜底 → 500 且原因被替换成
        "Internal server error."，用户只看到"失败了"，不知道该改什么。"""
        from fastapi import FastAPI

        from bianbu_agent.api.errors import install_error_handlers

        app = FastAPI()
        install_error_handlers(app)
        assert ProviderHostRejected in app.exception_handlers, \
            "未注册处理器 —— 策略拒绝会变成 500 且原因被吞掉"
