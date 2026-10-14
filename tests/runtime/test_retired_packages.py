from pathlib import Path
from types import SimpleNamespace

from bianbu_agent import BianbuAgentRuntime, RuntimeConfig


def test_control_cleans_only_retired_packages_after_all_runs_finish(
    tmp_path: Path,
) -> None:
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=None,
        agent_id="default",
        role="control",
    )
    retired = runtime.home.staging_dir / "retired-skill-review-1"
    recovery = runtime.home.staging_dir / "skill-rejected-review-1"
    retired.mkdir()
    recovery.mkdir()

    runtime.runs = SimpleNamespace(active_count=1)
    assert runtime.cleanup_retired_packages() == 0
    assert retired.is_dir()

    runtime.runs = SimpleNamespace(active_count=0)
    assert runtime.cleanup_retired_packages() == 1
    assert not retired.exists()
    assert recovery.is_dir()


def test_control_keeps_retired_packages_for_a_stale_worker(tmp_path: Path) -> None:
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=None,
        agent_id="default",
        role="control",
    )
    retired = runtime.home.staging_dir / "retired-mcp-example-1"
    retired.mkdir()
    runtime.runs = SimpleNamespace(active_count=0)
    runtime.worker_supervisor = SimpleNamespace(
        _supervisors={"first": SimpleNamespace(catalog_stale=True)}
    )

    assert runtime.cleanup_retired_packages() == 0
    assert retired.is_dir()

    runtime.worker_supervisor._supervisors["first"].catalog_stale = False
    assert runtime.cleanup_retired_packages() == 1
    assert not retired.exists()
