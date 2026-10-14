from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


def sync_ui(source_root: Path, destination: Path) -> dict[str, Any]:
    """Vendor the product console and record reproducible file hashes."""

    source_root = Path(source_root).resolve()
    destination = Path(destination).resolve()
    source_web = source_root / "channel" / "web"
    required = (
        source_web / "chat.html",
        source_web / "static",
        source_root / "LICENSE",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"missing product console inputs: {', '.join(missing)}")

    destination.mkdir(parents=True, exist_ok=True)
    (destination / "sdk-chat.html").unlink(missing_ok=True)
    shutil.copy2(source_web / "chat.html", destination / "chat.html")
    shutil.copytree(
        source_web / "static",
        destination / "static",
        dirs_exist_ok=True,
    )
    shutil.copy2(source_root / "LICENSE", destination / "UPSTREAM_LICENSE")

    return refresh_manifest(destination)


def refresh_manifest(destination: Path) -> dict[str, Any]:
    """Record hashes for the one complete vendored product console."""

    destination = Path(destination).resolve()
    files: dict[str, str] = {}
    tracked = [destination / "chat.html", destination / "UPSTREAM_LICENSE"]
    tracked.extend(sorted((destination / "static").rglob("*")))
    for path in tracked:
        if path.is_file():
            relative = path.relative_to(destination).as_posix()
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest: dict[str, Any] = {
        "branding": "Bianbu Agent",
        "files": files,
    }
    (destination / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
