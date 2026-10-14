from pathlib import Path

import pytest

from bianbu_agent.tools import ToolLoadContext, ToolPackageContext


def test_load_context_has_no_tool_owned_data_or_permissions(tmp_path: Path) -> None:
    context = ToolLoadContext(
        workspace_root=tmp_path / "workspace",
        services={"runtime_factory": object()},
    )

    assert context.workspace_root == (tmp_path / "workspace").resolve()
    assert not hasattr(context, "data_root")
    assert not hasattr(context, "approved_permissions")


def test_package_context_exposes_only_static_package_paths(tmp_path: Path) -> None:
    package = ToolPackageContext(
        name="echo_value",
        package_root=tmp_path / "echo_value",
        source_digest="a" * 64,
        workspace_root=tmp_path / "workspace",
    )
    assert not hasattr(package, "runtime")
    assert not hasattr(package, "runtime_factory")
    assert package.name == "echo_value"
    assert package.package_root == (tmp_path / "echo_value").resolve()
    assert package.workspace_root == (tmp_path / "workspace").resolve()
    assert not hasattr(package, "manifest")
    assert package.data_root == (tmp_path / "echo_value/data").resolve()
    assert not hasattr(package, "resources_root")


def test_package_context_validates_name_and_digest(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="name"):
        ToolPackageContext("Wrong-Name", tmp_path, "a" * 64, tmp_path)
    with pytest.raises(ValueError, match="SHA-256"):
        ToolPackageContext("valid_name", tmp_path, "short", tmp_path)
