from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SDK_TOOLS = PROJECT_ROOT / "bianbu_agent" / "tools"
PACKAGES = PROJECT_ROOT / ".bianbu-agent" / "tools"


def test_product_business_logic_remains_with_its_tool_directory() -> None:
    runtime = (SDK_TOOLS / "runtime_services.py").read_text(encoding="utf-8")

    forbidden_runtime_markers = {}
    for name, marker in forbidden_runtime_markers.items():
        root = PACKAGES / name
        package_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(root.rglob("*.py"))
            if "__pycache__" not in path.parts
        )
        assert marker not in runtime, f"{name} business logic remains in SDK runtime"
        assert marker in package_source, f"{name} package does not own its behavior"
