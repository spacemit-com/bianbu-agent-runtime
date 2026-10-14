from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def load_package_module(
    root: Path,
    module_name: str,
    *,
    namespace: str,
) -> ModuleType:
    """Load one capability module under an isolated synthetic package."""

    root = Path(root).resolve()
    parts = module_name.split(".")
    module_path = root.joinpath(*parts).with_suffix(".py")
    if not module_path.is_file():
        raise ValueError(f"capability module does not exist: {module_name}")
    package_name = namespace
    _namespace_package(package_name, root)
    current_root = root
    for part in parts[:-1]:
        current_root = current_root / part
        package_name = f"{package_name}.{part}"
        _namespace_package(package_name, current_root)
    full_name = f"{namespace}.{module_name}"
    cached = sys.modules.get(full_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(full_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load capability module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    try:
        source = module_path.read_bytes()
        exec(compile(source, str(module_path), "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(full_name, None)
        raise
    return module


def _namespace_package(name: str, root: Path) -> None:
    if name in sys.modules:
        return
    module = ModuleType(name)
    module.__package__ = name
    module.__path__ = [str(root)]
    sys.modules[name] = module


__all__ = ["load_package_module"]
