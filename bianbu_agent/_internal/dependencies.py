"""Validation of Python dependencies declared by in-process capabilities."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version


def validate_python_requirements(root: Path) -> tuple[str, ...]:
    """Validate an optional requirements.txt against this Python environment.

    The host deliberately validates only. It never invokes pip, follows include
    files, or accepts URLs/options that could alter the host environment.
    """

    path = Path(root).expanduser().resolve() / "requirements.txt"
    if not path.exists():
        return ()
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"requirements.txt must be a regular file: {path}")

    requirements: list[tuple[str, Requirement]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"unable to read requirements.txt {path}: {exc}") from exc
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.partition("#")[0].strip()
        if not line:
            continue
        if line.startswith(("-", "+")) or " @ " in line:
            raise ValueError(
                f"unsupported requirements.txt directive at line {line_number}: {raw_line!r}"
            )
        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            raise ValueError(
                f"invalid requirement in requirements.txt at line {line_number}: {raw_line!r}"
            ) from exc
        if requirement.url is not None:
            raise ValueError(
                f"URL requirements are not supported in requirements.txt at line {line_number}"
            )
        normalized = str(requirement)
        requirements.append((normalized, requirement))

    unsatisfied: list[str] = []
    for normalized, requirement in requirements:
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        try:
            installed = Version(metadata.version(requirement.name))
        except metadata.PackageNotFoundError:
            unsatisfied.append(f"{normalized} (not installed)")
            continue
        except InvalidVersion:
            unsatisfied.append(f"{normalized} (installed version is invalid)")
            continue
        if requirement.specifier and installed not in requirement.specifier:
            unsatisfied.append(
                f"{normalized} (installed version {installed} does not satisfy it)"
            )
    if unsatisfied:
        raise ValueError(
            f"unsatisfied requirements for {path.parent}: " + "; ".join(unsatisfied)
        )
    return tuple(item[0] for item in requirements)


__all__ = ["validate_python_requirements"]
