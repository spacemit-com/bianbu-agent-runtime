from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_registrar():
    path = Path(__file__).parents[2] / "debian" / "register-security-manifest.py"
    spec = importlib.util.spec_from_file_location("runtime_manifest_registrar", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _package(root: Path, *, complete: bool = True) -> Path:
    package = root / "com.bianbu.agent"
    package.mkdir()
    (package / "manifest.toml").write_text("manifest", encoding="utf-8")
    if complete:
        (package / "manifest.toml.sig").write_text("signature", encoding="ascii")
        (package / "pubkey.hex").write_text("public", encoding="ascii")
    return package


def test_registrar_rejects_incomplete_package(tmp_path: Path):
    module = _load_registrar()
    with pytest.raises(RuntimeError, match="incomplete"):
        module.register(_package(tmp_path, complete=False))


def test_registrar_fails_closed_when_securityd_rejects(tmp_path: Path, monkeypatch):
    module = _load_registrar()
    package = _package(tmp_path)

    class Client:
        @staticmethod
        def install_package(_path):
            return {"ok": False, "reason": "bad signature", "agent_id": "", "count": 0}

    monkeypatch.setattr(module, "_client", lambda: Client)
    with pytest.raises(RuntimeError, match="bad signature"):
        module.register(package)


def test_registrar_requires_expected_runtime_identity(tmp_path: Path, monkeypatch):
    module = _load_registrar()
    package = _package(tmp_path)

    class Client:
        @staticmethod
        def install_package(_path):
            return {"ok": True, "reason": "", "agent_id": "other.agent", "count": 1}

    monkeypatch.setattr(module, "_client", lambda: Client)
    with pytest.raises(RuntimeError, match="unexpected agent"):
        module.register(package, expected_agent_id="com.bianbu.agent")


def test_registrar_can_accept_independent_agent_identity(tmp_path: Path, monkeypatch):
    module = _load_registrar()
    package = _package(tmp_path)

    class Client:
        @staticmethod
        def install_package(_path):
            return {"ok": True, "reason": "", "agent_id": "com.vendor.research", "count": 2}

    monkeypatch.setattr(module, "_client", lambda: Client)
    result = module.register(package, expected_agent_id="com.vendor.research")
    assert result["agent_id"] == "com.vendor.research"
def test_optional_registration_warns_and_succeeds_without_security_client(
    tmp_path: Path, monkeypatch, capsys
):
    module = _load_registrar()
    package = _package(tmp_path)

    def unavailable():
        raise RuntimeError("bianbu_security_client is not installed")

    monkeypatch.setattr(module, "_client", unavailable)

    assert module.main(["--optional", str(package)]) == 0
    assert "Security Runtime registration deferred" in capsys.readouterr().err


def test_registrar_discovers_current_security_package_client(tmp_path: Path):
    """The DEB installs the client under the securityd package's published path."""
    module = _load_registrar()
    client_root = tmp_path / "usr/lib/bianbu-agent-securityd/client"
    client_root.mkdir(parents=True)
    (client_root / "bianbu_security_client.py").write_text("", encoding="utf-8")
    assert module._find_client_dir([str(client_root)]) == str(client_root)


def test_registrar_reports_missing_security_package_client(tmp_path: Path):
    module = _load_registrar()
    with pytest.raises(RuntimeError, match="Security client module not found"):
        module._find_client_dir([str(tmp_path / "missing")])


@pytest.mark.parametrize('existing', [None, {'status': 'active', 'agent_id': 'com.bianbu.runtime'}])
def test_startup_restores_only_missing_manifest(tmp_path, monkeypatch, existing):
    module = _load_registrar()
    calls = []
    class Client:
        @staticmethod
        def get_agent_info(agent_id):
            assert agent_id == 'com.bianbu.runtime'
            return {'manifest': existing}
        @staticmethod
        def install_package(path):
            calls.append(path)
            return {'ok': True, 'agent_id': 'com.bianbu.runtime', 'count': 22}
    monkeypatch.setattr(module, '_client', lambda: Client)
    assert module.main([str(_package(tmp_path)), '--expected-agent-id', 'com.bianbu.runtime', '--if-missing']) == 0
    assert len(calls) == (1 if existing is None else 0)


def test_startup_does_not_reactivate_suspended_runtime(tmp_path, monkeypatch):
    module = _load_registrar()
    class Client:
        @staticmethod
        def get_agent_info(agent_id):
            return {'manifest': {'agent_id': agent_id, 'status': 'suspended'}}
        @staticmethod
        def install_package(path):
            pytest.fail('must not overwrite an existing administrator decision')
    monkeypatch.setattr(module, '_client', lambda: Client)
    with pytest.raises(RuntimeError, match='suspended'):
        module.main([str(_package(tmp_path)), '--expected-agent-id', 'com.bianbu.runtime', '--if-missing'])
