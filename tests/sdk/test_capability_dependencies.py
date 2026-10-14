from pathlib import Path

import pytest

from bianbu_agent._internal.dependencies import validate_python_requirements


def _requirements(tmp_path: Path, content: str) -> Path:
    root = tmp_path / "capability"
    root.mkdir()
    (root / "requirements.txt").write_text(content, encoding="utf-8")
    return root


def test_absent_requirements_are_valid(tmp_path: Path) -> None:
    root = tmp_path / "capability"
    root.mkdir()

    assert validate_python_requirements(root) == ()


def test_satisfied_and_inactive_requirements_are_normalized(tmp_path: Path) -> None:
    root = _requirements(
        tmp_path,
        "# package dependencies\n"
        "packaging>=24  # supplied by the host\n"
        "definitely-missing-bianbu-package; python_version < '2'\n",
    )

    assert validate_python_requirements(root) == (
        "packaging>=24",
        'definitely-missing-bianbu-package; python_version < "2"',
    )


def test_missing_and_incompatible_requirements_are_reported_together(
    tmp_path: Path,
) -> None:
    root = _requirements(
        tmp_path,
        "definitely-missing-bianbu-package>=1\npackaging<1\n",
    )

    with pytest.raises(ValueError) as captured:
        validate_python_requirements(root)

    message = str(captured.value)
    assert "definitely-missing-bianbu-package>=1" in message
    assert "packaging<1" in message
    assert "not installed" in message
    assert "installed version" in message


@pytest.mark.parametrize(
    "content",
    (
        "not a valid requirement ???\n",
        "-r other.txt\n",
        "--index-url https://example.invalid/simple\n",
        "demo @ https://example.invalid/demo.whl\n",
    ),
)
def test_invalid_or_installing_requirements_are_rejected(
    tmp_path: Path,
    content: str,
) -> None:
    root = _requirements(tmp_path, content)

    with pytest.raises(ValueError, match="requirements.txt"):
        validate_python_requirements(root)
