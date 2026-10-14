from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from bianbu_agent.observability import (
    RuntimeLogStore,
    bind_log_context,
    current_log_context,
    log_run_terminal,
)


def test_log_context_is_nested_and_task_safe() -> None:
    assert current_log_context() == {}
    with bind_log_context(agent_id="com.bianbu.alpha", run_id="run-1"):
        assert current_log_context() == {
            "agent_id": "com.bianbu.alpha",
            "run_id": "run-1",
        }
        with bind_log_context(provider_request_id="provider-1"):
            assert current_log_context()["provider_request_id"] == "provider-1"
        assert "provider_request_id" not in current_log_context()
    assert current_log_context() == {}


def test_failed_log_write_does_not_dump_original_credentials(tmp_path, capsys):
    store = RuntimeLogStore(tmp_path / "runtime.jsonl")
    # An actual invalid destination exercises the failed append, not a mock.
    store.path.mkdir()
    record = logging.LogRecord(
        "bianbu_agent.test", logging.ERROR, __file__, 1,
        "password=private-credential", (), None,
    )
    store.handler.emit(record)
    error = capsys.readouterr().err
    assert "private-credential" not in error
    assert "could not be persisted" in error


@pytest.mark.parametrize("message", [
    '{"password": "private credential with spaces", "status": "failed"}',
    "{'api_key': 'private credential with spaces', 'status': 'failed'}",
    'password="private credential with spaces" status=failed',
])
def test_log_store_redacts_quoted_credentials(tmp_path, message):
    store = RuntimeLogStore(tmp_path / "runtime.jsonl")
    store.handler.emit(logging.LogRecord(
        "bianbu_agent.test", logging.ERROR, __file__, 1, message, (), None,
    ))
    persisted = store.path.read_text()
    assert "private" not in persisted
    assert "credential" not in persisted
    assert "spaces" not in persisted
    assert "failed" in persisted


def test_log_files_are_private_even_with_permissive_umask(tmp_path):
    store = RuntimeLogStore(tmp_path / "runtime.jsonl", max_bytes=512)
    previous = os.umask(0)
    try:
        for _ in range(5):
            store.handler.emit(logging.LogRecord(
                "bianbu_agent.test", logging.INFO, __file__, 1, "x" * 250, (), None,
            ))
    finally:
        os.umask(previous)
    for path in (store.path, store.lock_path, tmp_path / "runtime.jsonl.1"):
        assert path.stat().st_mode & 0o777 == 0o600


def test_runtime_log_store_redacts_and_filters_correlated_records(tmp_path: Path) -> None:
    store = RuntimeLogStore(tmp_path / "runtime.jsonl", max_bytes=64 * 1024)
    logger = logging.getLogger("bianbu_agent.test.correlated")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(store.handler)
    try:
        with bind_log_context(
            agent_id="com.bianbu.alpha",
            session_id="session-1",
            run_id="run-1",
            plugin_generation=7,
        ):
            logger.info(
                "provider failed Authorization: Bearer super-secret-token "
                "api_key=sk-abcdefghijklmnopqrstuvwxyz"
            )
            log_run_terminal(logger, status="failed", error_code="provider_error")
        with bind_log_context(agent_id="com.bianbu.beta", run_id="run-2"):
            logger.info("healthy")
    finally:
        logger.removeHandler(store.handler)
        store.close()

    alpha = store.query(agent_id="com.bianbu.alpha", limit=20)
    run = store.query(run_id="run-1", limit=20)
    assert alpha == run
    assert len(alpha) == 2
    assert alpha[0]["plugin_generation"] == 7
    assert alpha[0]["session_id"] == "session-1"
    assert "super-secret-token" not in alpha[0]["message"]
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in alpha[0]["message"]
    assert "[REDACTED]" in alpha[0]["message"]
    assert alpha[1]["event"] == "run_terminal"
    assert alpha[1]["run_status"] == "failed"
    assert alpha[1]["error_code"] == "provider_error"
    assert store.query(agent_id="com.bianbu.beta", limit=20)[0]["message"] == "healthy"


def test_runtime_log_store_rotates_without_returning_partial_json(tmp_path: Path) -> None:
    store = RuntimeLogStore(tmp_path / "runtime.jsonl", max_bytes=800)
    logger = logging.getLogger("bianbu_agent.test.rotation")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(store.handler)
    try:
        with bind_log_context(agent_id="com.bianbu.alpha"):
            for index in range(30):
                logger.info("record-%02d-%s", index, "x" * 40)
    finally:
        logger.removeHandler(store.handler)
        store.close()

    records = store.query(agent_id="com.bianbu.alpha", limit=100)
    assert records
    assert all(record["message"].startswith("record-") for record in records)
    assert (tmp_path / "runtime.jsonl.1").is_file()


def test_runtime_log_store_installs_once_and_uninstalls_cleanly(tmp_path: Path) -> None:
    store = RuntimeLogStore(tmp_path / "runtime.jsonl")
    logger = logging.getLogger("bianbu_agent.test.install")
    original_level = logger.level
    logger.setLevel(logging.NOTSET)
    logger.propagate = False

    store.install(logger)
    store.install(logger)
    logger.info("once")
    store.uninstall(logger)
    logger.info("not persisted")

    assert [record["message"] for record in store.query(limit=10)] == ["once"]
    assert store.handler not in logger.handlers
    assert logger.level == logging.NOTSET
    logger.setLevel(original_level)
