from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import signal
import stat
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from bianbu_agent.agents import AgentModel, AgentSpec, JSONAgentStore
from bianbu_agent.errors import (
    ModelRecordIntegrityError,
    ModelRecordRollbackError,
    ModelResolutionError,
)
from bianbu_agent.models import YAMLModelResolver, YAMLModelStore, ModelConfig
from bianbu_agent.models import file_store as file_store_module
from bianbu_agent.models import file_resolver as file_resolver_module
from bianbu_agent.sessions import JSONSessionStore


def _replace_chat_provider_in_process(
    root: Path,
    ready: multiprocessing.synchronize.Event,
    mutate_now: multiprocessing.synchronize.Event,
    attempting: multiprocessing.synchronize.Event,
    result: multiprocessing.queues.Queue,
) -> None:
    async def mutate() -> None:
        store = YAMLModelStore(root)
        await store.open()
        ready.set()
        if not mutate_now.wait(timeout=10):
            result.put(("error", "mutation was not released"))
            return
        attempting.set()
        try:
            deleted = await store.delete("custom-provider-chat")
            await store.create(ModelConfig(
                id="custom-provider-chat",
                provider="custom:chat",
                model="credential",
                base_url="https://replacement.test/v1",
                parameters={
                    "record_kind": "custom_provider",
                    "api_key": "replacement-secret",
                },
            ))
        except BaseException as exc:
            result.put(("error", type(exc).__name__))
        else:
            result.put(("success", deleted))
        finally:
            await store.close()

    asyncio.run(mutate())


_JOURNAL_PREPARE_BOUNDARIES = (
    "journal:temporary-write",
    "journal:temporary-fsync",
    "journal:publish-link",
    "journal:directory-fsync",
    "journal:temporary-remove",
    "journal:temporary-remove-directory-fsync",
)
_JOURNAL_COMMIT_BOUNDARIES = (
    "journal:remove",
    "journal:remove-directory-fsync",
)
_CREATE_CRASH_BOUNDARIES = (
    *_JOURNAL_PREPARE_BOUNDARIES,
    "custom-provider-chat:temporary-write",
    "custom-provider-chat:temporary-fsync",
    "custom-provider-chat:publish-link",
    "custom-provider-chat:directory-fsync",
    "capability-chat:temporary-write",
    "capability-chat:temporary-fsync",
    "capability-chat:publish-link",
    "capability-chat:directory-fsync",
    *_JOURNAL_COMMIT_BOUNDARIES,
)
_UPDATE_CRASH_BOUNDARIES = (
    *_JOURNAL_PREPARE_BOUNDARIES,
    "custom-provider-chat:temporary-write",
    "custom-provider-chat:temporary-fsync",
    "custom-provider-chat:publish-replace",
    "custom-provider-chat:directory-fsync",
    "capability-chat:temporary-write",
    "capability-chat:temporary-fsync",
    "capability-chat:publish-replace",
    "capability-chat:directory-fsync",
    *_JOURNAL_COMMIT_BOUNDARIES,
)
_CREATE_RECOVERY_CRASH_BOUNDARIES = (
    "capability-chat:publish-delete",
    "capability-chat:delete-directory-fsync",
    "custom-provider-chat:publish-delete",
    "custom-provider-chat:delete-directory-fsync",
    *_JOURNAL_COMMIT_BOUNDARIES,
)
_UPDATE_RECOVERY_CRASH_BOUNDARIES = (
    "capability-chat:temporary-write",
    "capability-chat:temporary-fsync",
    "capability-chat:publish-replace",
    "capability-chat:directory-fsync",
    "custom-provider-chat:temporary-write",
    "custom-provider-chat:temporary-fsync",
    "custom-provider-chat:publish-replace",
    "custom-provider-chat:directory-fsync",
    *_JOURNAL_COMMIT_BOUNDARIES,
)


def _crash_project_capability_transaction(
    root: Path,
    operation: str,
    boundary: str,
) -> None:
    async def run() -> None:
        store = YAMLModelStore(root)
        await store.open()
        if operation == "update":
            await store.upsert_project_capability(
                capability="chat",
                model="old-chat",
                base_url="https://old-chat.test/v1",
                api_key="old-chat-secret",
            )

        def crash_at_boundary(actual: str) -> None:
            if actual == boundary:
                os.kill(os.getpid(), signal.SIGKILL)

        if hasattr(file_store_module, "_durability_boundary"):
            file_store_module._durability_boundary = crash_at_boundary
        else:
            method_name = "replace" if operation == "update" else "create"
            original_write = getattr(store._records, method_name)

            def crash_after_legacy_provider_write(record_id, payload):
                result = original_write(record_id, payload)
                if str(record_id) == "custom-provider-chat":
                    os.kill(os.getpid(), signal.SIGKILL)
                return result

            setattr(store._records, method_name, crash_after_legacy_provider_write)

        if operation == "create":
            await store.create_project_capability(
                capability="chat",
                model="new-chat",
                base_url="https://new-chat.test/v1",
                api_key="new-chat-secret",
            )
        else:
            await store.upsert_project_capability(
                capability="chat",
                model="new-chat",
                base_url="https://new-chat.test/v1",
                api_key="new-chat-secret",
            )

    asyncio.run(run())


def _start_and_kill_at_transaction_boundary(
    root: Path,
    operation: str,
    boundary: str,
) -> None:
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_project_capability_transaction,
        args=(root, operation, boundary),
    )
    process.start()
    process.join(timeout=15)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
        pytest.fail(f"transaction child did not reach boundary: {boundary}")
    assert process.exitcode == -signal.SIGKILL


def _crash_project_capability_recovery(root: Path, boundary: str) -> None:
    def crash_at_boundary(actual: str) -> None:
        if actual == boundary:
            os.kill(os.getpid(), signal.SIGKILL)

    file_store_module._durability_boundary = crash_at_boundary
    if hasattr(file_resolver_module, "_durability_boundary"):
        file_resolver_module._durability_boundary = crash_at_boundary
    YAMLModelResolver(root).resolve_capability("chat")


def _start_and_kill_recovery_at_boundary(root: Path, boundary: str) -> None:
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_project_capability_recovery,
        args=(root, boundary),
    )
    process.start()
    process.join(timeout=15)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
        pytest.fail(f"recovery child did not reach boundary: {boundary}")
    assert process.exitcode == -signal.SIGKILL


def _assert_no_transaction_artifacts(root: Path) -> None:
    artifacts = {
        path.name
        for path in root.iterdir()
        if path.name == ".model-records.transaction"
        or path.name.startswith(".model-transaction-")
        or path.name.startswith(".record-")
    }
    assert artifacts == set()


@pytest.mark.parametrize("boundary", _CREATE_CRASH_BOUNDARIES)
def test_project_capability_create_recovers_every_crash_boundary(
    tmp_path: Path,
    boundary: str,
) -> None:
    root = tmp_path / boundary.replace(":", "-") / "models"
    _start_and_kill_at_transaction_boundary(root, "create", boundary)

    resolved = YAMLModelResolver(root).resolve_capability("chat")
    if resolved is None:

        async def bootstrap() -> None:
            store = YAMLModelStore(root)
            await store.open()
            try:
                await store.create_project_capability(
                    capability="chat",
                    model="restart-chat",
                    base_url="https://restart-chat.test/v1",
                    api_key="restart-chat-secret",
                )
            finally:
                await store.close()

        asyncio.run(bootstrap())
        resolved = YAMLModelResolver(root).resolve_capability(
            "chat", required=True
        )
        assert resolved is not None
        assert (
            resolved.model,
            resolved.base_url,
            resolved.api_key,
        ) == (
            "restart-chat",
            "https://restart-chat.test/v1",
            "restart-chat-secret",
        )
    else:
        assert (
            resolved.model,
            resolved.base_url,
            resolved.api_key,
        ) == (
            "new-chat",
            "https://new-chat.test/v1",
            "new-chat-secret",
        )
    _assert_no_transaction_artifacts(root)


@pytest.mark.parametrize("boundary", _UPDATE_CRASH_BOUNDARIES)
def test_project_capability_update_recovers_every_crash_boundary(
    tmp_path: Path,
    boundary: str,
) -> None:
    root = tmp_path / boundary.replace(":", "-") / "models"
    _start_and_kill_at_transaction_boundary(root, "update", boundary)

    resolved = YAMLModelResolver(root).resolve_capability("chat", required=True)

    assert resolved is not None
    assert (
        resolved.model,
        resolved.base_url,
        resolved.api_key,
    ) in {
        ("old-chat", "https://old-chat.test/v1", "old-chat-secret"),
        ("new-chat", "https://new-chat.test/v1", "new-chat-secret"),
    }
    _assert_no_transaction_artifacts(root)


@pytest.mark.parametrize("boundary", _CREATE_RECOVERY_CRASH_BOUNDARIES)
def test_project_capability_create_recovery_survives_second_crash(
    tmp_path: Path,
    boundary: str,
) -> None:
    root = tmp_path / boundary.replace(":", "-") / "models"
    _start_and_kill_at_transaction_boundary(
        root,
        "create",
        "capability-chat:directory-fsync",
    )
    _start_and_kill_recovery_at_boundary(root, boundary)

    assert YAMLModelResolver(root).resolve_capability("chat") is None
    _assert_no_transaction_artifacts(root)


@pytest.mark.parametrize("boundary", _UPDATE_RECOVERY_CRASH_BOUNDARIES)
def test_project_capability_update_recovery_survives_second_crash(
    tmp_path: Path,
    boundary: str,
) -> None:
    root = tmp_path / boundary.replace(":", "-") / "models"
    _start_and_kill_at_transaction_boundary(
        root,
        "update",
        "capability-chat:directory-fsync",
    )
    _start_and_kill_recovery_at_boundary(root, boundary)

    resolved = YAMLModelResolver(root).resolve_capability("chat", required=True)
    assert resolved is not None
    assert (
        resolved.model,
        resolved.base_url,
        resolved.api_key,
    ) == (
        "old-chat",
        "https://old-chat.test/v1",
        "old-chat-secret",
    )
    _assert_no_transaction_artifacts(root)


def test_hand_written_legacy_capability_pair_resolves_but_is_never_claimed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    provider_dir = root / "custom-chat"
    provider_dir.mkdir(parents=True)
    provider_path = provider_dir / "provider.yaml"
    capability_path = provider_dir / "legacy-chat.yaml"
    provider_path.write_text(
        yaml.safe_dump(
            {
                "id": "custom-provider-chat",
                "schema_version": 1,
                "provider": "custom:chat",
                "model": "credential",
                "base_url": "https://legacy-chat.test/v1",
                "parameters": {
                    "record_kind": "custom_provider",
                    "custom_id": "chat",
                    "name": "Chat Provider",
                    "api_key": "legacy-chat-secret",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    capability_path.write_text(
        yaml.safe_dump(
            {
                "id": "capability-chat",
                "schema_version": 1,
                "provider": "custom:chat",
                "model": "legacy-chat",
                "base_url": "",
                "parameters": {"capability": "chat"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    provider_path.chmod(0o600)
    capability_path.chmod(0o600)
    original_documents = (provider_path.read_bytes(), capability_path.read_bytes())

    initial = YAMLModelResolver(root).resolve_capability("chat", required=True)

    assert initial is not None
    assert (
        initial.model,
        initial.base_url,
        initial.api_key,
    ) == (
        "legacy-chat",
        "https://legacy-chat.test/v1",
        "legacy-chat-secret",
    )
    assert (provider_path.read_bytes(), capability_path.read_bytes()) == (
        original_documents
    )

    async def upgrade() -> None:
        store = YAMLModelStore(root)
        await store.open()
        try:
            with pytest.raises(
                ModelRecordIntegrityError,
                match="collides with a user-owned record",
            ):
                await store.upsert_project_capability(
                    capability="chat",
                    model="upgraded-chat",
                )
        finally:
            await store.close()

    asyncio.run(upgrade())
    surviving = YAMLModelResolver(root).resolve_capability("chat", required=True)
    assert surviving is not None
    assert (
        surviving.model,
        surviving.base_url,
        surviving.api_key,
    ) == (
        "legacy-chat",
        "https://legacy-chat.test/v1",
        "legacy-chat-secret",
    )
    assert (provider_path.read_bytes(), capability_path.read_bytes()) == (
        original_documents
    )
    _assert_no_transaction_artifacts(root)


@pytest.mark.asyncio
async def test_store_open_removes_owned_provider_orphan_from_prejournal_create(
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    provider_dir = root / "custom-chat"
    provider_dir.mkdir(parents=True)
    orphan_path = provider_dir / "provider.yaml"
    orphan_path.write_text(
        yaml.safe_dump(
            {
                "id": "custom-provider-chat",
                "schema_version": 1,
                "provider": "custom:chat",
                "model": "credential",
                "base_url": "https://orphan-chat.test/v1",
                "parameters": {
                    "record_kind": "project_capability_provider",
                    "capability": "chat",
                    "owner": "capability-chat",
                    "name": "Chat Provider",
                    "api_key": "orphan-chat-secret",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    orphan_path.chmod(0o600)
    store = YAMLModelStore(root)

    await store.open()
    provider, capability = await store.create_project_capability(
        capability="chat",
        model="restart-chat",
        base_url="https://restart-chat.test/v1",
        api_key="restart-chat-secret",
    )

    assert provider.id == "custom-provider-chat"
    assert capability.id == "capability-chat"
    assert await store.get(provider.id) == provider
    assert await store.get(capability.id) == capability


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["store", "resolver"])
@pytest.mark.parametrize("journal_kind", ["malformed", "insecure", "symlink"])
async def test_model_transaction_journal_fails_closed_without_secret_disclosure(
    tmp_path: Path,
    entrypoint: str,
    journal_kind: str,
) -> None:
    root = tmp_path / "models"
    store = YAMLModelStore(root)
    await store.open()
    await store.create_project_capability(
        capability="chat",
        model="stable-chat",
        base_url="https://stable-chat.test/v1",
        api_key="stable-chat-secret",
    )
    await store.close()
    journal_path = root / ".model-records.transaction"
    if journal_kind == "symlink":
        target = root / ".synthetic-journal-target"
        target.write_text("symlink-journal-secret", encoding="utf-8")
        target.chmod(0o600)
        journal_path.symlink_to(target.name)
    else:
        journal_path.write_text(
            '{"synthetic":"corrupt-journal-secret"}\n',
            encoding="utf-8",
        )
        journal_path.chmod(0o644 if journal_kind == "insecure" else 0o600)

    with pytest.raises(ModelResolutionError) as failure:
        if entrypoint == "store":
            restarted = YAMLModelStore(root)
            await restarted.open()
        else:
            YAMLModelResolver(root).resolve_capability("chat", required=True)

    message = str(failure.value)
    assert "transaction journal" in message
    assert "stable-chat-secret" not in message
    assert "corrupt-journal-secret" not in message
    assert "symlink-journal-secret" not in message


@pytest.mark.asyncio
async def test_model_transaction_journal_rejects_missing_before_snapshots(
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    store = YAMLModelStore(root)
    await store.open()
    await store.create_project_capability(
        capability="chat",
        model="stable-chat",
        base_url="https://stable-chat.test/v1",
        api_key="stable-chat-secret",
    )
    provider_path = root / "custom-chat" / "provider.yaml"
    capability_path = root / "custom-chat" / "stable-chat.yaml"
    before = (provider_path.read_bytes(), capability_path.read_bytes())
    journal_path = root / ".model-records.transaction"
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "project_capability_pair",
                "provider_id": "custom-provider-chat",
                "capability_id": "capability-chat",
                "before": {},
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    journal_path.chmod(0o600)

    with pytest.raises(ModelResolutionError) as failure:
        YAMLModelResolver(root).resolve_capability("chat", required=True)

    assert str(failure.value) == "invalid Model transaction journal"
    assert (provider_path.read_bytes(), capability_path.read_bytes()) == before


@pytest.mark.asyncio
async def test_model_store_create_project_capability_writes_private_pair(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()

    provider, capability = await store.create_project_capability(
        capability="chat",
        model="new-chat",
        base_url="https://chat.create.test/v1",
        api_key="create-secret",
    )

    assert await store.get(provider.id) == provider
    assert await store.get(capability.id) == capability
    paths = tuple((tmp_path / "models").rglob("*.yaml"))
    assert {path.name for path in paths} == {
        "new-chat.yaml",
        "provider.yaml",
    }
    assert {stat.S_IMODE(path.stat().st_mode) for path in paths} == {0o600}
    transaction_lock = tmp_path / "models" / ".model-records.lock"
    assert stat.S_IMODE(transaction_lock.stat().st_mode) == 0o600
    assert {item.id for item in await store.list()} == {
        "capability-chat",
        "custom-provider-chat",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["provider", "capability"])
async def test_model_store_create_project_capability_rejects_either_collision(
    tmp_path: Path,
    collision: str,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    if collision == "provider":
        await store.create(ModelConfig(
            id="custom-provider-chat",
            provider="custom:chat",
            model="credential",
            base_url="https://existing.test/v1",
            parameters={"record_kind": "custom_provider", "api_key": "existing-secret"},
        ))
    else:
        await store.create(ModelConfig(
            id="custom-provider-other",
            provider="custom:other",
            model="credential",
            base_url="https://existing.test/v1",
            parameters={"record_kind": "custom_provider", "api_key": "existing-secret"},
        ))
        await store.create(ModelConfig(
            id="capability-chat",
            provider="custom:other",
            model="existing-chat",
            parameters={"capability": "chat"},
        ))
    before = {
        path.relative_to(tmp_path / "models"): path.read_bytes()
        for path in (tmp_path / "models").rglob("*.yaml")
    }

    with pytest.raises(ValueError, match="already exists"):
        await store.create_project_capability(
            capability="chat",
            model="replacement-chat",
            base_url="https://replacement.test/v1",
            api_key="replacement-secret",
        )

    assert {
        path.relative_to(tmp_path / "models"): path.read_bytes()
        for path in (tmp_path / "models").rglob("*.yaml")
    } == before


@pytest.mark.asyncio
async def test_model_store_create_project_capability_compensates_second_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    original_create = store._records.create

    def fail_capability_create(record_id, payload):
        if str(record_id) == "capability-chat":
            raise OSError("injected capability create failure")
        return original_create(record_id, payload)

    monkeypatch.setattr(store._records, "create", fail_capability_create)

    with pytest.raises(OSError, match="injected capability create failure"):
        await store.create_project_capability(
            capability="chat",
            model="new-chat",
            base_url="https://chat.create.test/v1",
            api_key="create-secret",
        )

    assert not tuple((tmp_path / "models").rglob("*.yaml"))


@pytest.mark.asyncio
async def test_model_store_create_project_capability_reports_compensation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    original_create = store._records.user.create

    def fail_capability_create(record_id, payload):
        if str(record_id) == "capability-chat":
            raise OSError("primary-create-secret failure")
        return original_create(record_id, payload)

    def fail_provider_delete(record_id):
        if str(record_id) == "custom-provider-chat":
            raise OSError("rollback-create-secret failure")
        raise AssertionError(f"unexpected rollback target: {record_id}")

    monkeypatch.setattr(store._records.user, "create", fail_capability_create)
    monkeypatch.setattr(store._records.user, "delete", fail_provider_delete)

    with pytest.raises(ModelRecordRollbackError) as failure:
        await store.create_project_capability(
            capability="chat",
            model="new-chat",
            base_url="https://chat.create.test/v1",
            api_key="create-secret",
        )

    assert failure.value.record_ids == ("custom-provider-chat",)
    assert "create-secret" not in str(failure.value)
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    rendered = "".join(traceback.format_exception(failure.value))
    assert "primary-create-secret" not in rendered
    assert "rollback-create-secret" not in rendered


def test_model_store_create_project_capability_is_atomic_across_store_instances(
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    stores = (YAMLModelStore(root), YAMLModelStore(root))
    barrier = threading.Barrier(2)

    def bootstrap(index: int) -> tuple[str, object]:
        async def run() -> tuple[str, object]:
            store = stores[index]
            await store.open()
            try:
                barrier.wait(timeout=5)
                value = await store.create_project_capability(
                    capability="chat",
                    model=f"chat-{index}",
                    base_url=f"https://chat-{index}.test/v1",
                    api_key=f"secret-{index}",
                )
            except BaseException as exc:
                return "error", exc
            finally:
                await store.close()
            return "success", value

        return asyncio.run(run())

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(bootstrap, range(2)))

    assert [status for status, _ in results].count("success") == 1
    assert [status for status, _ in results].count("error") == 1
    loser = next(value for status, value in results if status == "error")
    assert isinstance(loser, ValueError)
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*.yaml")
    }
    assert set(before) == {
        Path("custom-chat/chat-0.yaml"),
        Path("custom-chat/provider.yaml"),
    } or set(before) == {
        Path("custom-chat/chat-1.yaml"),
        Path("custom-chat/provider.yaml"),
    }
    reader = YAMLModelStore(root)

    async def read_pair() -> tuple[ModelConfig | None, ModelConfig | None]:
        await reader.open()
        try:
            return (
                await reader.get("custom-provider-chat"),
                await reader.get("capability-chat"),
            )
        finally:
            await reader.close()

    provider, capability = asyncio.run(read_pair())
    assert provider is not None
    assert capability is not None
    assert provider.parameters["api_key"] in {"secret-0", "secret-1"}
    winner = str(provider.parameters["api_key"])[-1]
    assert capability.model == f"chat-{winner}"
    after = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*.yaml")
    }
    assert set(after) == set(before)
    assert provider.parameters["record_kind"] == "project_capability_provider"
    assert provider.parameters["owner"] == "capability-chat"
    assert capability.parameters["capability"] == "chat"


@pytest.mark.parametrize("capability_write_fails", [False, True])
def test_project_capability_transaction_excludes_cross_process_provider_replacement(
    tmp_path: Path,
    capability_write_fails: bool,
) -> None:
    """Deleting/recreating the claim cannot enter a two-record transaction."""

    root = tmp_path / "models"
    store = YAMLModelStore(root)
    provider_claimed = threading.Event()
    release_capability_write = threading.Event()
    original_create = store._records.create

    def paused_create(record_id, payload):
        if str(record_id) == "capability-chat":
            provider_claimed.set()
            if not release_capability_write.wait(timeout=10):
                raise TimeoutError("test did not release capability write")
            if capability_write_fails:
                raise OSError("injected capability write failure")
        return original_create(record_id, payload)

    store._records.create = paused_create

    def create_pair() -> tuple[str, object]:
        async def run() -> tuple[str, object]:
            await store.open()
            try:
                pair = await store.create_project_capability(
                    capability="chat",
                    model="original-chat",
                    base_url="https://original.test/v1",
                    api_key="original-secret",
                )
            except BaseException as exc:
                return "error", exc
            finally:
                await store.close()
            return "success", pair

        return asyncio.run(run())

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    mutate_now = context.Event()
    attempting = context.Event()
    process_result = context.Queue()
    replacer = context.Process(
        target=_replace_chat_provider_in_process,
        args=(root, ready, mutate_now, attempting, process_result),
    )
    replacer.start()
    # Importing the spawned test module takes about six seconds on K3/RISC-V.
    assert ready.wait(timeout=15)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pair_future = executor.submit(create_pair)
        assert provider_claimed.wait(timeout=5)
        try:
            mutate_now.set()
            assert attempting.wait(timeout=5)
            replacer.join(timeout=0.3)
            replacement_was_excluded = replacer.is_alive()
        finally:
            release_capability_write.set()
        pair_status, pair_value = pair_future.result(timeout=10)
        replacer.join(timeout=10)

    assert replacement_was_excluded
    assert not replacer.is_alive()
    replacement_status, replacement_value = process_result.get(timeout=2)

    reader = YAMLModelStore(root)

    async def read_pair() -> tuple[ModelConfig | None, ModelConfig | None]:
        await reader.open()
        try:
            return (
                await reader.get("custom-provider-chat"),
                await reader.get("capability-chat"),
            )
        finally:
            await reader.close()

    provider, capability = asyncio.run(read_pair())
    if capability_write_fails:
        assert pair_status == "error"
        assert isinstance(pair_value, OSError)
        assert replacement_status == "success"
        assert replacement_value is False
        assert provider is not None
        assert provider.parameters["api_key"] == "replacement-secret"
        assert capability is None
    else:
        assert pair_status == "success"
        assert replacement_status == "error"
        assert replacement_value == "ModelRecordIntegrityError"
        assert provider is not None
        assert provider.parameters["api_key"] == "original-secret"
        assert capability is not None
        assert capability.model == "original-chat"


def test_model_resolver_waits_for_complete_project_capability_transaction(
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    store = YAMLModelStore(root)
    provider_claimed = threading.Event()
    release_capability_write = threading.Event()
    resolver_started = threading.Event()
    original_create = store._records.create

    def paused_create(record_id, payload):
        if str(record_id) == "capability-chat":
            provider_claimed.set()
            if not release_capability_write.wait(timeout=10):
                raise TimeoutError("test did not release capability write")
        return original_create(record_id, payload)

    store._records.create = paused_create

    def create_pair() -> None:
        async def run() -> None:
            await store.open()
            try:
                await store.create_project_capability(
                    capability="chat",
                    model="snapshot-chat",
                    base_url="https://snapshot.test/v1",
                    api_key="snapshot-secret",
                )
            finally:
                await store.close()

        asyncio.run(run())

    def resolve_pair():
        resolver_started.set()
        return YAMLModelResolver(root).resolve_capability("chat", required=True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(create_pair)
        assert provider_claimed.wait(timeout=5)
        resolver = executor.submit(resolve_pair)
        assert resolver_started.wait(timeout=5)
        time.sleep(0.1)
        resolver_was_excluded = not resolver.done()
        release_capability_write.set()
        writer.result(timeout=10)
        resolved = resolver.result(timeout=10)

    assert resolver_was_excluded
    assert resolved is not None
    assert resolved.model == "snapshot-chat"
    assert resolved.api_key == "snapshot-secret"
    assert resolved.base_url == "https://snapshot.test/v1"


@pytest.mark.asyncio
async def test_agent_json_and_model_provider_yaml_are_deterministic_files(
    tmp_path: Path,
) -> None:
    agents = JSONAgentStore(tmp_path / "agents")
    models = YAMLModelStore(tmp_path / "models")
    await agents.open()
    await models.open()

    spec = AgentSpec(
        id="writer",
        name="Writer",
        model=AgentModel("openai-compatible", "demo-chat"),
        tools=("clock",),
        permissions={"level": "L1"},
    )
    config = ModelConfig(
        id="cloud",
        provider="openai-compatible",
        model="demo-chat",
        base_url="https://example.invalid/v1",
    )
    assert await agents.create(spec) == spec
    assert await models.create(config) == config

    assert (await agents.get("writer")) == spec
    assert (await models.get("cloud")) == config
    agent_payload = json.loads((tmp_path / "agents" / "writer.json").read_text())
    model_path = tmp_path / "models" / "openai-compatible" / "demo-chat.yaml"
    model_payload = yaml.safe_load(model_path.read_text())
    assert agent_payload["schema_version"] == 1
    assert model_payload["schema_version"] == 1
    assert "api_key" not in model_payload
    assert (tmp_path / "agents" / "writer.json").read_text().endswith("\n")


@pytest.mark.asyncio
async def test_json_stores_reject_unsafe_ids_casefold_conflicts_and_bad_schema(
    tmp_path: Path,
) -> None:
    store = JSONAgentStore(tmp_path / "agents")
    await store.open()
    await store.create(AgentSpec(id="Writer", name="Writer"))

    with pytest.raises(ValueError, match="case-insensitive"):
        await store.create(AgentSpec(id="writer", name="Second"))
    with pytest.raises(ValueError, match="invalid"):
        await store.get("../writer")

    path = tmp_path / "agents" / "Writer.json"
    payload = json.loads(path.read_text())
    payload["schema_version"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        await store.get("Writer")


@pytest.mark.asyncio
async def test_json_sessions_backfill_legacy_agent_binding(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    store = JSONSessionStore(root)
    await store.open()
    await store.create("legacy", agent_id="default", title="Legacy")
    await store.close()

    path = root / "legacy" / "projection.json"
    document = json.loads(path.read_text())
    document["record"].pop("agent_id")
    path.write_text(json.dumps(document), encoding="utf-8")

    reopened = JSONSessionStore(root)
    await reopened.open()
    await reopened.backfill_agent_id("default")
    restored = await reopened.get("legacy")
    assert restored is not None
    assert restored.agent_id == "default"
    await reopened.close()


@pytest.mark.asyncio
async def test_model_store_rejects_deleting_referenced_provider(tmp_path: Path) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await store.create(ModelConfig(
        id="custom-provider-chat",
        provider="custom:chat",
        model="credential",
        base_url="https://models.example.test/v1",
        parameters={"record_kind": "custom_provider", "api_key": "secret"},
    ))
    await store.create(ModelConfig(
        id="capability-chat",
        provider="custom:chat",
        model="qwen-chat",
        parameters={"capability": "chat"},
    ))

    with pytest.raises(ModelResolutionError) as failure:
        await store.delete("custom-provider-chat")

    message = str(failure.value)
    assert "custom-provider-chat" in message
    assert "capability-chat" in message
    assert "secret" not in message
    assert await store.get("custom-provider-chat") is not None


@pytest.mark.asyncio
async def test_model_store_deletes_chat_capability_without_default_special_case(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await store.create(ModelConfig(
        id="custom-provider-chat",
        provider="custom:chat",
        model="credential",
        base_url="https://models.example.test/v1",
        parameters={"record_kind": "custom_provider", "api_key": "secret"},
    ))
    await store.create(ModelConfig(
        id="capability-chat",
        provider="custom:chat",
        model="qwen-chat",
        parameters={"capability": "chat"},
    ))

    deleted = await store.delete("capability-chat")

    assert deleted is True
    assert await store.get("capability-chat") is None


@pytest.mark.asyncio
async def test_model_store_rejects_invalid_chat_capability_replacement(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await store.create(ModelConfig(
        id="custom-provider-chat",
        provider="custom:chat",
        model="credential",
        base_url="https://models.example.test/v1",
        parameters={"record_kind": "custom_provider", "api_key": "secret"},
    ))
    original = ModelConfig(
        id="capability-chat",
        provider="custom:chat",
        model="qwen-chat",
        parameters={"capability": "chat"},
    )
    await store.create(original)

    with pytest.raises(ModelResolutionError) as failure:
        await store.update(ModelConfig(
            id="capability-chat",
            provider="custom:missing",
            model="replacement-chat",
            parameters={"capability": "chat", "api_key": "must-not-leak"},
        ))

    message = str(failure.value)
    assert "capability-chat" in message
    assert "custom-provider-missing" in message
    assert "must-not-leak" not in message
    assert await store.get("capability-chat") == original


@pytest.mark.asyncio
async def test_model_store_rejects_creating_dangling_capability(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()

    with pytest.raises(ModelResolutionError) as failure:
        await store.create(ModelConfig(
            id="capability-vision",
            provider="custom:missing",
            model="qwen-vision",
            parameters={"capability": "vision", "api_key": "must-not-leak"},
        ))

    message = str(failure.value)
    assert "capability-vision" in message
    assert "custom-provider-missing" in message
    assert "must-not-leak" not in message
    assert await store.get("capability-vision") is None


@pytest.mark.asyncio
async def test_model_store_rejects_record_kind_on_reserved_capability_create(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await store.create(ModelConfig(
        id="custom-provider-chat",
        provider="custom:chat",
        model="credential",
        base_url="https://models.example.test/v1",
        parameters={"record_kind": "custom_provider", "api_key": "secret"},
    ))

    with pytest.raises(ModelResolutionError) as failure:
        await store.create(ModelConfig(
            id="capability-chat",
            provider="custom:chat",
            model="qwen-chat",
            parameters={
                "capability": "chat",
                "record_kind": "option",
                "api_key": "must-not-leak",
            },
        ))

    message = str(failure.value)
    assert "capability-chat" in message
    assert "must-not-leak" not in message
    assert await store.get("capability-chat") is None


@pytest.mark.asyncio
async def test_model_store_rejects_record_kind_capability_update_and_keeps_provider_protected(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    provider = ModelConfig(
        id="custom-provider-chat",
        provider="custom:chat",
        model="credential",
        base_url="https://models.example.test/v1",
        parameters={"record_kind": "custom_provider", "api_key": "secret"},
    )
    capability = ModelConfig(
        id="capability-chat",
        provider="custom:chat",
        model="qwen-chat",
        parameters={"capability": "chat"},
    )
    await store.create(provider)
    await store.create(capability)

    with pytest.raises(ModelResolutionError) as update_failure:
        await store.update(ModelConfig(
            id="capability-chat",
            provider="custom:missing",
            model="replacement-chat",
            parameters={"capability": "chat", "record_kind": "option"},
        ))

    assert "capability-chat" in str(update_failure.value)
    assert await store.get("capability-chat") == capability
    with pytest.raises(ModelResolutionError) as delete_failure:
        await store.delete(provider.id)
    assert "custom-provider-chat" in str(delete_failure.value)
    assert "capability-chat" in str(delete_failure.value)
    assert await store.get(provider.id) == provider


@pytest.mark.asyncio
async def test_model_store_rejects_incomplete_referenced_provider_update(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    original = ModelConfig(
        id="custom-provider-chat",
        provider="custom:chat",
        model="credential",
        base_url="https://models.example.test/v1",
        parameters={"record_kind": "custom_provider", "api_key": "secret"},
    )
    await store.create(original)
    await store.create(ModelConfig(
        id="capability-chat",
        provider="custom:chat",
        model="qwen-chat",
        parameters={"capability": "chat"},
    ))

    with pytest.raises(ModelResolutionError) as failure:
        await store.update(ModelConfig(
            id=original.id,
            provider=original.provider,
            model=original.model,
            base_url=original.base_url,
            parameters={"record_kind": "custom_provider", "api_key": ""},
        ))

    message = str(failure.value)
    assert "custom-provider-chat" in message
    assert "API key" in message
    assert "secret" not in message
    assert await store.get("custom-provider-chat") == original


@pytest.mark.asyncio
async def test_model_store_secret_file_permissions_are_0600_after_create_and_update(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    provider = ModelConfig(
        id="custom-provider-chat",
        provider="custom:chat",
        model="credential",
        base_url="https://models.example.test/v1",
        parameters={"record_kind": "custom_provider", "api_key": "first-secret"},
    )

    await store.create(provider)
    path = tmp_path / "models" / "custom-chat" / "provider.yaml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    path.chmod(0o644)
    await store.update(ModelConfig(
        id=provider.id,
        provider=provider.provider,
        model=provider.model,
        base_url=provider.base_url,
        parameters={"record_kind": "custom_provider", "api_key": "second-secret"},
    ))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_model_store_get_and_list_reject_unsafe_record_permissions(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await store.create(ModelConfig(
        id="option-secret",
        provider="console",
        model="enabled",
        parameters={"record_kind": "option", "api_key": "must-not-leak"},
    ))
    (tmp_path / "models" / "console" / "enabled.yaml").chmod(0o644)

    with pytest.raises(ModelResolutionError) as get_failure:
        await store.get("option-secret")
    assert str(get_failure.value) == (
        "Model record permissions unsafe: option-secret"
    )

    with pytest.raises(ModelResolutionError) as list_failure:
        await store.list()
    assert str(list_failure.value) == (
        "Model record permissions unsafe: option-secret"
    )


@pytest.mark.asyncio
async def test_model_store_never_claims_a_legacy_shaped_project_provider(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    legacy_provider = ModelConfig(
        id="custom-provider-chat",
        provider="custom:chat",
        model="credential",
        base_url="https://legacy-chat.test/v1",
        parameters={
            "record_kind": "custom_provider",
            "custom_id": "chat",
            "name": "Chat Provider",
            "api_key": "legacy-chat-secret",
        },
    )
    await store.create(legacy_provider)
    await store.create(ModelConfig(
        id="capability-chat",
        provider="custom:chat",
        model="legacy-chat",
        parameters={"capability": "chat"},
    ))

    with pytest.raises(
        ModelRecordIntegrityError,
        match="collides with a user-owned record",
    ):
        await store.upsert_project_capability(
            capability="chat",
            model="new-chat",
        )

    assert await store.get("custom-provider-chat") == legacy_provider
    assert await store.get("capability-chat") == ModelConfig(
        id="capability-chat",
        provider="custom:chat",
        model="legacy-chat",
        parameters={"capability": "chat"},
    )


@pytest.mark.asyncio
async def test_model_store_rejects_removing_project_provider_ownership(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    provider, _ = await store.upsert_project_capability(
        capability="chat",
        model="owned-chat",
        base_url="https://owned-chat.test/v1",
        api_key="owned-chat-secret",
    )

    with pytest.raises(
        ModelRecordIntegrityError,
        match="ownership metadata cannot be removed",
    ):
        await store.update(ModelConfig(
            id=provider.id,
            provider=provider.provider,
            model=provider.model,
            base_url=provider.base_url,
            parameters={
                "record_kind": "custom_provider",
                "custom_id": "chat",
                "api_key": "replacement-secret",
            },
        ))

    assert await store.get(provider.id) == provider


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_model_store_project_capability_batch_compensates_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    if existing:
        await store.upsert_project_capability(
            capability="image",
            model="old-image",
            base_url="https://old-image.test/v1",
            api_key="old-image-secret",
        )
    before_provider = await store.get("custom-provider-image")
    before_capability = await store.get("capability-image")

    method_name = "replace" if existing else "create"
    original_write = getattr(store._records, method_name)
    failed = False

    def fail_capability_once(record_id, payload):
        nonlocal failed
        if str(record_id) == "capability-image" and not failed:
            failed = True
            raise OSError("injected capability write failure")
        return original_write(record_id, payload)

    monkeypatch.setattr(store._records, method_name, fail_capability_once)

    with pytest.raises(OSError, match="injected capability write failure"):
        await store.upsert_project_capability(
            capability="image",
            model="new-image",
            base_url="https://new-image.test/v1",
            api_key="new-image-secret",
        )

    assert await store.get("custom-provider-image") == before_provider
    assert await store.get("capability-image") == before_capability


@pytest.mark.asyncio
async def test_model_store_batch_restores_provider_after_persistent_capability_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await store.upsert_project_capability(
        capability="image",
        model="old-image",
        base_url="https://old-image.test/v1",
        api_key="old-image-secret",
    )
    before_provider = await store.get("custom-provider-image")
    before_capability = await store.get("capability-image")
    original_replace = store._records.replace

    def fail_capability_replace(record_id, payload):
        if str(record_id) == "capability-image":
            raise OSError("persistent capability replace failure")
        return original_replace(record_id, payload)

    monkeypatch.setattr(store._records, "replace", fail_capability_replace)

    with pytest.raises(OSError, match="persistent capability replace failure"):
        await store.upsert_project_capability(
            capability="image",
            model="new-image",
            base_url="https://new-image.test/v1",
            api_key="new-image-secret",
        )

    assert await store.get("custom-provider-image") == before_provider
    assert await store.get("capability-image") == before_capability


@pytest.mark.asyncio
async def test_model_store_batch_attempts_both_restores_and_reports_rollback_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await store.upsert_project_capability(
        capability="image",
        model="old-image",
        base_url="https://old-image.test/v1",
        api_key="old-image-secret",
    )
    before_provider = await store.get("custom-provider-image")
    original_replace = store._records.user.replace
    capability_calls = 0
    provider_calls = 0

    def fail_capability_write_and_rollback(record_id, payload):
        nonlocal capability_calls, provider_calls
        if str(record_id) == "custom-provider-image":
            provider_calls += 1
            return original_replace(record_id, payload)
        if str(record_id) == "capability-image":
            capability_calls += 1
            if capability_calls == 1:
                original_replace(record_id, payload)
                raise OSError("new-image-secret primary write failure")
            raise OSError("old-image-secret rollback failure")
        return original_replace(record_id, payload)

    monkeypatch.setattr(
        store._records.user,
        "replace",
        fail_capability_write_and_rollback,
    )

    with pytest.raises(ModelRecordRollbackError) as failure:
        await store.upsert_project_capability(
            capability="image",
            model="new-image",
            base_url="https://new-image.test/v1",
            api_key="new-image-secret",
        )

    assert provider_calls == 2
    # Reads retry the durable recovery journal; a persistent storage fault
    # must remain visible until the user-layer write operation recovers.
    with pytest.raises(ModelRecordRollbackError):
        await store.get("custom-provider-image")
    monkeypatch.setattr(store._records.user, "replace", original_replace)
    assert await store.get("custom-provider-image") == before_provider
    assert "new-image-secret" not in str(failure.value)
    assert "old-image-secret" not in str(failure.value)
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    rendered = "".join(traceback.format_exception(failure.value))
    assert "new-image-secret" not in rendered
    assert "old-image-secret" not in rendered
