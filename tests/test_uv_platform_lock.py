from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


PROJECT_ROOT = Path(__file__).parents[1]
RISCV64_MARKER = "sys_platform == 'linux' and platform_machine == 'riscv64'"
REQUIRED_ENVIRONMENTS = {
    RISCV64_MARKER,
}
SPACEMIT_NATIVE_PACKAGES = {
    "cffi",
    "cryptography",
    "markupsafe",
    "orjson",
    "ormsgpack",
    "pillow",
    "playwright",
    "psutil",
    "pyyaml",
    "ruamel-yaml-clib",
    "sqlite-vec",
    "tiktoken",
    "uuid-utils",
    "zstandard",
}


def _toml(path: str) -> dict:
    return tomllib.loads((PROJECT_ROOT / path).read_text(encoding="utf-8"))


def test_locked_project_metadata_resolves_without_sibling_repositories(
    tmp_path: Path,
) -> None:
    """A clean checkout must be runnable without unpublished sibling sources."""

    shutil.copy2(PROJECT_ROOT / "pyproject.toml", tmp_path)
    shutil.copy2(PROJECT_ROOT / "uv.lock", tmp_path)

    completed = subprocess.run(
        ["uv", "lock", "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_uv_sources_select_spacemit_only_for_riscv64_native_packages() -> None:
    project = _toml("pyproject.toml")
    uv = project["tool"]["uv"]

    assert set(uv["required-environments"]) == REQUIRED_ENVIRONMENTS
    indexes = {item["name"]: item for item in uv["index"]}
    assert indexes == {
        "spacemit": {
            "name": "spacemit",
            "url": "https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple",
            "explicit": True,
        },
        "tuna": {
            "name": "tuna",
            "url": "https://pypi.tuna.tsinghua.edu.cn/simple",
            "default": True,
        },
    }
    assert set(uv["sources"]) == SPACEMIT_NATIVE_PACKAGES
    for package in SPACEMIT_NATIVE_PACKAGES:
        assert uv["sources"][package] == [
            {"index": "spacemit", "marker": RISCV64_MARKER}
        ]


def test_every_spacemit_package_is_a_direct_riscv64_dependency() -> None:
    project = _toml("pyproject.toml")
    requirements = [
        Requirement(value)
        for value in [
            *project["project"]["dependencies"],
            *project["dependency-groups"]["dev"],
        ]
    ]

    for python_version in ("3.14",):
        active = {
            canonicalize_name(requirement.name)
            for requirement in requirements
            if requirement.marker is None
            or requirement.marker.evaluate({
                "python_version": python_version,
                "python_full_version": f"{python_version}.0",
                "sys_platform": "linux",
                "platform_machine": "riscv64",
            })
        }
        assert SPACEMIT_NATIVE_PACKAGES <= active


def _supports_python(wheel_url: str, python_minor: int) -> bool:
    filename = wheel_url.rsplit("/", 1)[-1]
    if "-py3-none-" in filename or f"-cp3{python_minor}-cp3{python_minor}-" in filename:
        return True
    abi3 = re.search(r"-cp3(\d+)-abi3-", filename)
    return abi3 is not None and int(abi3.group(1)) <= python_minor


def test_lock_contains_spacemit_riscv64_wheels_for_supported_k3_python() -> None:
    lock = _toml("uv.lock")
    wheels_by_package: dict[str, list[str]] = {}
    for package in lock["package"]:
        if package.get("source", {}).get("registry", "").startswith(
            "https://git.spacemit.com/"
        ):
            wheels_by_package.setdefault(package["name"], []).extend(
                wheel["url"] for wheel in package.get("wheels", [])
            )

    for python_minor in (14,):
        missing = sorted(
            package
            for package in SPACEMIT_NATIVE_PACKAGES
            if not any(
                "riscv64" in url and _supports_python(url, python_minor)
                for url in wheels_by_package.get(package, [])
            )
        )
        assert missing == [], f"missing CPython 3.{python_minor} riscv64 wheels"
