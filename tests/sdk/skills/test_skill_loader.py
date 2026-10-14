from pathlib import Path

import pytest

from bianbu_agent.inventory import CapabilityOrigin
from bianbu_agent.skills import DirectorySkillLoader
from bianbu_agent.skills.loader import _parse_skill


def _write_skill(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\n"
        "name: research\n"
        "display_name: Research Assistant\n"
        "description: Find primary sources\n"
        "version: 1.2.0\n"
        "---\n"
        "Use primary sources and cite them.",
        encoding="utf-8",
    )
    references = root / "references"
    references.mkdir()
    (references / "guide.md").write_text("trusted guide", encoding="utf-8")


def test_skill_loader_builds_standalone_contribution_and_provenance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills" / "research"
    _write_skill(root)

    contribution = DirectorySkillLoader().load(
        root,
        origin=CapabilityOrigin.STANDALONE,
    )

    assert contribution.plugin_id == "bianbu.catalog.filesystem"
    assert contribution.spec.name == "research"
    assert contribution.spec.display_name == "Research Assistant"
    assert contribution.spec.description == "Find primary sources"
    assert contribution.spec.version == "1.2.0"
    assert contribution.spec.instructions == "Use primary sources and cite them."
    assert contribution.spec.resources == {
        "references/guide.md": str((root / "references" / "guide.md").resolve())
    }
    assert contribution.spec.metadata["origin"] == "standalone"
    assert contribution.spec.metadata["plugin_id"] is None
    assert contribution.spec.metadata["source_path"] == str(
        (root / "SKILL.md").resolve()
    )
    assert contribution.spec.metadata["manifest_path"] == str(
        (root / "SKILL.md").resolve()
    )
    digest = contribution.spec.metadata["content_digest"]
    assert isinstance(digest, str)
    assert len(digest) == 64


def test_skill_loader_reads_power_format_v2_display_fields(tmp_path: Path) -> None:
    """A v2 Skill keeps ``name`` as the call name and adds display names."""

    root = tmp_path / "skills" / "using-apm"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\n"
        "schema_version: 2\n"
        "name: using-apm\n"
        "name_zh: APM 使用手册\n"
        "name_en: APM Agent Handbook\n"
        "description: Manage Powers with APM.\n"
        "description_zh: 使用 APM 管理 Power 资源。\n"
        "version: v1.2.0\n"
        "---\n"
        "Use the apm CLI.",
        encoding="utf-8",
    )

    spec = DirectorySkillLoader().load(root).spec

    assert spec.name == "using-apm"
    assert spec.display_name == "using-apm"
    assert spec.name_zh == "APM 使用手册"
    assert spec.name_en == "APM Agent Handbook"
    assert spec.description_zh == "使用 APM 管理 Power 资源。"
    assert spec.metadata["name_zh"] == "APM 使用手册"
    assert spec.metadata["name_en"] == "APM Agent Handbook"
    assert spec.metadata["description_zh"] == "使用 APM 管理 Power 资源。"


def test_skill_loader_defaults_missing_display_fields_to_empty(tmp_path: Path) -> None:
    """Skills written before v2 keep loading; clients fall back to ``name``."""

    root = tmp_path / "skills" / "research"
    _write_skill(root)

    spec = DirectorySkillLoader().load(root).spec

    assert spec.name_zh == ""
    assert spec.name_en == ""
    assert spec.description_zh == ""
    assert spec.metadata["name_zh"] == ""


def test_skill_loader_tracks_plugin_owner_and_has_deterministic_digest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plugins" / "vendor.system" / "skills" / "research"
    _write_skill(root)
    loader = DirectorySkillLoader()

    first = loader.load(
        root,
        origin=CapabilityOrigin.PLUGIN,
        plugin_id="vendor.system",
    )
    second = loader.load(
        root,
        origin=CapabilityOrigin.PLUGIN,
        plugin_id="vendor.system",
    )

    assert first.plugin_id == "vendor.system"
    assert first.spec.metadata["origin"] == "plugin"
    assert first.spec.metadata["plugin_id"] == "vendor.system"
    assert first.spec.metadata["content_digest"] == second.spec.metadata[
        "content_digest"
    ]

    (root / "references" / "guide.md").write_text("changed", encoding="utf-8")
    changed = loader.load(
        root,
        origin=CapabilityOrigin.PLUGIN,
        plugin_id="vendor.system",
    )
    assert changed.spec.metadata["content_digest"] != first.spec.metadata[
        "content_digest"
    ]


def test_skill_loader_rejects_missing_or_malformed_manifest(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    missing.mkdir()
    with pytest.raises(ValueError, match="SKILL.md is required"):
        DirectorySkillLoader().load(missing)

    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / "SKILL.md").write_text("---\nname: demo\nno closing marker")
    with pytest.raises(ValueError, match="frontmatter is not closed"):
        DirectorySkillLoader().load(malformed)


@pytest.mark.parametrize(
    "frontmatter",
    (
        "---\nname: demo\n---\nInstructions",
        "---\nname: demo\ndescription:\n---\nInstructions",
    ),
)
def test_skill_loader_requires_non_empty_description(
    tmp_path: Path,
    frontmatter: str,
) -> None:
    root = tmp_path / "skill"
    root.mkdir()
    (root / "SKILL.md").write_text(frontmatter, encoding="utf-8")

    with pytest.raises(ValueError, match="frontmatter description is required"):
        DirectorySkillLoader().load(root)


def test_skill_loader_rejects_symlinks_that_can_escape_the_skill(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills" / "research"
    _write_skill(root)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "references" / "escape.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="symbolic links are not allowed"):
        DirectorySkillLoader().load(root)


def test_skill_loader_rejects_plugin_origin_without_an_owner(tmp_path: Path) -> None:
    root = tmp_path / "skill"
    _write_skill(root)

    with pytest.raises(ValueError, match="plugin_id is required"):
        DirectorySkillLoader().load(root, origin=CapabilityOrigin.PLUGIN)


# The published spacemit-power Skills carry frontmatter written by their vendor
# packages: folded scalars (``>-``) and nested ``metadata:``/``compatibility:``
# blocks.  A line-oriented parser rejects those outright, so the loader has to
# read the frontmatter as YAML.
_VENDOR_VPU_BENCHMARK = (
    "---\n"
    "name: vpu-benchmark\n"
    'version: "0.13.9"\n'
    "description: >-\n"
    "  测试 SpacemiT K3 VPU 硬件视频编解码器的基准性能、FPS、吞吐和 MVX 带宽，\n"
    "  支持已注册的 H.264/H.265/VP8/VP9/MJPEG 单路与多路用例。\n"
    "compatibility: >-\n"
    "  Requires Linux on SpacemiT K3, Python 3.11+, MVX VPU tools.\n"
    "metadata:\n"
    '  version: "0.13.9"\n'
    '  entrypoint: "scripts/run.py"\n'
    '  request_argument: "--request-json"\n'
    '  timeout_seconds: "1500"\n'
    "---\n"
    "Run the benchmark."
)

_VENDOR_MEDIA_PIPELINE = (
    "---\n"
    "name: k3-media-pipeline\n"
    'version: "v0.7.0"\n'
    "description: >-\n"
    "  在 K3 板卡上使用 GStreamer 或 FFmpeg 查看媒体信息，执行视频播放、硬件编解码、\n"
    "  多路硬件解码、摄像头预览/录制/拍照、抽帧、RTSP播放/录制以及文件或摄像头推流。\n"
    "metadata:\n"
    '  version: "0.7.0"\n'
    '  target: "spacemit-k3"\n'
    "---\n"
    "Run the pipeline."
)


@pytest.mark.parametrize(
    ("frontmatter", "name", "version", "description"),
    (
        (
            _VENDOR_VPU_BENCHMARK,
            "vpu-benchmark",
            "0.13.9",
            "测试 SpacemiT K3 VPU 硬件视频编解码器的基准性能、FPS、吞吐和 MVX 带宽， "
            "支持已注册的 H.264/H.265/VP8/VP9/MJPEG 单路与多路用例。",
        ),
        (
            _VENDOR_MEDIA_PIPELINE,
            "k3-media-pipeline",
            "v0.7.0",
            "在 K3 板卡上使用 GStreamer 或 FFmpeg 查看媒体信息，执行视频播放、硬件编解码、 "
            "多路硬件解码、摄像头预览/录制/拍照、抽帧、RTSP播放/录制以及文件或摄像头推流。",
        ),
    ),
)
def test_skill_loader_reads_vendor_frontmatter_as_yaml(
    tmp_path: Path,
    frontmatter: str,
    name: str,
    version: str,
    description: str,
) -> None:
    root = tmp_path / "skills" / name
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(frontmatter, encoding="utf-8")

    metadata, instructions = _parse_skill(frontmatter)

    assert metadata["name"] == name
    assert metadata["version"] == version
    assert metadata["description"] == description
    assert instructions == frontmatter.rsplit("---\n", 1)[1].strip()
    # Unknown top-level keys stay out of the record instead of failing the load.
    assert set(metadata) <= {"name", "display_name", "description", "version"}


def test_skill_loader_loads_vendor_style_skill_without_plugin_context(
    tmp_path: Path,
) -> None:
    """The same file fails standalone, so the frontmatter parser is at fault."""

    root = tmp_path / "skills" / "vpu-benchmark"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(_VENDOR_VPU_BENCHMARK, encoding="utf-8")

    contribution = DirectorySkillLoader().load(root)

    assert contribution.spec.name == "vpu-benchmark"
    assert contribution.spec.version == "0.13.9"
    assert contribution.spec.instructions == "Run the benchmark."


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    (
        ("---\nname: demo\ndescription: [unclosed\n---\nInstructions", "invalid SKILL.md frontmatter"),
        ("---\n- name\n- description\n---\nInstructions", "frontmatter must be a YAML mapping"),
        (
            "---\nname: demo\ndescription:\n  nested: mapping\n---\nInstructions",
            "frontmatter description must be a single string",
        ),
        ("---\nname: demo\nname: other\ndescription: demo\n---\nInstructions", "duplicate key"),
    ),
)
def test_skill_loader_rejects_broken_frontmatter(
    tmp_path: Path,
    frontmatter: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _parse_skill(frontmatter)
