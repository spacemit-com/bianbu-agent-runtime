"""Executable contract for the published resource-oriented HTTP API guide."""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

from bianbu_agent.server.app import create_app
from bianbu_agent.api.errors import _status_code


PROJECT_ROOT = Path(__file__).parents[1]
GUIDE = PROJECT_ROOT / "docs" / "api-guide.md"
README = PROJECT_ROOT / "README.md"
DOCS_ROOT = PROJECT_ROOT / "docs"
RETIRED_API_GUIDES = (
    DOCS_ROOT / "api-source-map.md",
    DOCS_ROOT / "fastapi-api-guide.md",
    DOCS_ROOT / "frontend-api-guide.md",
)

_ROUTE_LINE = re.compile(
    r"^(GET|POST|PUT|PATCH|DELETE) (/api/v1/\S+|/security-api/invoke)$"
)
_FLAT_API_PATH = re.compile(
    r"/api/(?!v1(?:/|(?=[^A-Za-z0-9_-])))"
)
_RETIRED_ACTIVE_DOC_PATH = re.compile(
    r"(?<![A-Za-z0-9_])(?:/api)?/(?:message|stream|poll)(?=$|[/?`\"')\]\s])"
    r"|/api/(?:health|version|chat|config|upload|file|history|messages/delete|"
    r"tools|skills|hooks|agent-packages|models|sessions|agents|runs|plugins|logs)"
    r"(?=$|[/?`\"')\]\s])"
)
_ARCHIVAL_DOC_DIRECTORIES = frozenset({"PR", "iter", "superpowers"})
_ERROR_TABLE_LINE = re.compile(
    r"^\| `(?P<code>[a-z_]+)` \| `(?P<status>\d{3})` \| .+ \|$"
)
_STABLE_ERROR_CODES = {
    "bad_request": 400,
    "forbidden": 403,
    "provider_host_rejected": 403,
    "not_found": 404,
    "conflict": 409,
    "run_conflict": 409,
    "idempotency_conflict": 409,
    "session_agent_mismatch": 409,
    "session_input_conflict": 409,
    "model_integrity_conflict": 409,
    "provider_in_use": 409,
    "mcp_already_installed": 409,
    "mcp_owned_by_plugin": 409,
    "mcp_activation_failed": 409,
    "mcp_reload_failed": 409,
    "plugin_activation_failed": 409,
    "plugin_reload_failed": 409,
    "plugin_managed_by_system": 409,
    "model_rollback_failed": 500,
    "internal_error": 500,
    "payload_too_large": 413,
    "unsupported_media_type": 415,
    "bad_gateway": 502,
    "validation_error": 422,
    "http_error": 405,
}


def _openapi_routes() -> list[tuple[str, str]]:
    """Return OpenAPI operations in the normalized route-index order."""

    app = create_app(cast(object, object()))
    document = app.openapi()
    return [
        (method.upper(), path)
        for path, operations in sorted(document["paths"].items())
        for method in sorted(operations)
        if method in {"get", "post", "put", "patch", "delete"}
    ]


def _documented_routes(text: str) -> list[tuple[str, str]]:
    return [
        (match.group(1), match.group(2))
        for line in text.splitlines()
        if (match := _ROUTE_LINE.fullmatch(line))
    ]


def _active_markdown_documents() -> tuple[Path, ...]:
    """Discover maintained docs while keeping intentional historical records archival."""

    return tuple(
        path
        for path in sorted(DOCS_ROOT.rglob("*.md"))
        if not _ARCHIVAL_DOC_DIRECTORIES.intersection(path.relative_to(DOCS_ROOT).parts)
    )


def _published_error_codes(text: str) -> dict[str, int]:
    return {
        match.group("code"): int(match.group("status"))
        for line in text.splitlines()
        if (match := _ERROR_TABLE_LINE.fullmatch(line))
    }


def test_api_guide_is_an_exact_inventory_of_the_final_openapi_contract() -> None:
    """The route index must list each current operation once and no retired route."""

    guide = GUIDE.read_text(encoding="utf-8")
    documented = _documented_routes(guide)

    assert documented
    assert len(documented) == len(set(documented))
    assert documented == _openapi_routes()
    assert _FLAT_API_PATH.search(guide) is None


def test_api_documentation_explains_namespaces_boundary_and_entry_points() -> None:
    """Readers have one canonical guide with the complete security boundary."""

    guide = GUIDE.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")

    assert "/api/v1" in guide
    assert "/api/admin/v1" not in guide
    assert "内置管理员 Bearer Token" in guide
    assert "破坏性变更" in guide
    assert "不提供兼容" in guide
    assert "单进程、单 worker" in guide
    assert "[HTTP API 指南](docs/api-guide.md)" in readme
    assert all(not path.exists() for path in RETIRED_API_GUIDES)


def test_api_guide_explains_cached_and_explicit_mcp_health_checks() -> None:
    """MCP clients can distinguish fast cached reads from explicit live checks."""

    guide = GUIDE.read_text(encoding="utf-8")
    documented = _documented_routes(guide)

    assert documented.count(("POST", "/api/v1/mcp-health-checks")) == 1
    assert "60 秒" in guide
    assert "stale-while-revalidate" in guide
    assert "GET /api/v1/mcp-servers" in guide
    assert "POST /api/v1/mcp-health-checks" in guide
    assert "不等待实时 MCP 进程启动" in guide
    assert "它会等待当前共享的实时检查" in guide
    assert "health_checked_at" in guide
    assert "刷新 MCP 状态" in guide


_PROVIDER_CAPTURE_OPERATIONS = [
    ("GET", "/api/v1/provider-capture", 200, "collection"),
    ("PUT", "/api/v1/provider-capture", 200, "collection"),
    ("DELETE", "/api/v1/provider-capture/records", 200, "collection"),
    ("DELETE", "/api/v1/provider-capture/target", 200, "collection"),
    ("GET", "/api/v1/provider-capture/{record_id}", 200, "detail"),
]
_PROVIDER_CAPTURE_ERRORS = [
    ("malformed_json", 400, "bad_request"),
    ("put_schema_invalid", 422, "validation_error"),
    ("provider_not_configured", 422, "validation_error"),
    ("provider_not_found", 404, "not_found"),
    ("record_id_invalid", 422, "validation_error"),
    ("record_not_found", 404, "not_found"),
]
_FRONTEND_CAPTURE_LIFECYCLE = [
    (
        "dedicated_page",
        "Monitoring > API Capture",
        "独立页面，与 Logs 分离",
    ),
    (
        "visible_single_flight_polling",
        "API Capture 页面可见期间",
        "轮询 collection，且最多一个请求 in flight",
    ),
    (
        "navigation_cleanup",
        "离开 API Capture 页面时",
        "停止轮询并 abort Provider、collection、detail、mutation 请求",
    ),
    (
        "generation_owned_mutation",
        "PUT/DELETE 进行中",
        "generation 拥有 AbortController，成功后重新读取 collection",
    ),
    (
        "open_detail_on_demand",
        "仅当操作员显式点击 Open 后",
        "请求该条记录的 detail",
    ),
    (
        "text_only_rendering",
        "全部 Provider 与 capture 内容",
        "仅用 textContent 或文本节点渲染，绝不使用 innerHTML",
    ),
]


def _provider_capture_section(text: str, heading: str) -> str:
    start = text.index(heading)
    match = re.search(r"^##? ", text[start + len(heading) :], re.MULTILINE)
    if match is None:
        return text[start:]
    return text[start : start + len(heading) + match.start()]


def _provider_capture_operations(section: str) -> list[tuple[str, str, int, str]]:
    pattern = re.compile(
        r"^\| `(GET|PUT|DELETE)` \| `(/api/v1/provider-capture[^`]*)` "
        r"\| `(200)` \| `(collection|detail)` \|$",
        re.MULTILINE,
    )
    return [
        (method, path, int(status), body)
        for method, path, status, body in pattern.findall(section)
    ]


def _provider_capture_errors(section: str) -> list[tuple[str, int, str]]:
    pattern = re.compile(
        r"^\| ([a-z_]+) \| `(400|404|422)` \| `([a-z_]+)` \|$",
        re.MULTILINE,
    )
    return [
        (scenario, int(status), code)
        for scenario, status, code in pattern.findall(section)
    ]


def _frontend_capture_lifecycle(section: str) -> list[tuple[str, str, str]]:
    scenarios = "|".join(item[0] for item in _FRONTEND_CAPTURE_LIFECYCLE)
    pattern = re.compile(
        rf"^\| ({scenarios}) \| `([^`]+)` \| `([^`]+)` \|$",
        re.MULTILINE,
    )
    return [tuple(row) for row in pattern.findall(section)]


def test_api_guide_publishes_the_provider_capture_api_and_storage_contract() -> None:
    """The guide specifies exact capture status, envelope, and disk contracts."""

    section = _provider_capture_section(
        GUIDE.read_text(encoding="utf-8"), "## Provider API Capture"
    )

    assert _provider_capture_operations(section) == _PROVIDER_CAPTURE_OPERATIONS
    assert _provider_capture_errors(section) == _PROVIDER_CAPTURE_ERRORS
    assert '{"data":' in section
    assert '{"error":' in section
    assert "Stop/Clear 都返回最新 `collection` envelope" in section
    assert "<data_dir>/logs/provider-capture/target.json" in section
    assert "<data_dir>/logs/provider-capture/records/<record_id>.json.gz" in section
    assert "<data_dir>/logs/provider-capture/records/<record_id>.preview.json" in section
    assert "<data_dir>/logs/provider-capture/records/index.json" in section


def test_api_guide_publishes_complete_provider_capture_operator_semantics() -> None:
    """The guide covers capture filtering, bounds, privacy, and UI behavior."""

    section = _provider_capture_section(
        GUIDE.read_text(encoding="utf-8"), "## Provider API Capture"
    )
    normalized = re.sub(r"\s+", " ", section)

    for clause in (
        "一个已配置 Provider 的规范 ID",
        "未 Start 和非目标 Provider 都是零写入",
        "跨服务重启保留",
        "in-flight",
        "Stop 保留记录；Clear 保留目标",
        "collection 只返回 preview，detail 才返回完整但有界的记录",
        "前 4 KiB 与后 4 KiB",
        "8 KiB 内可完整重建",
        "omitted_bytes",
        "8 MiB",
        "7 天或 100 MiB，以先到者为准",
        "gzip、preview sidecar 与 capture 临时文件的总量",
        "最多 1,000 个记录 pair",
        "sidecar 编码后最多 64 KiB",
        "过深结构会在写盘前替换为带类型的 `max-depth` 标记",
        "启动时和运行期间定期执行",
        "等待进行中的维护 worker 完成",
        "写盘前脱敏",
        "仍可能包含敏感",
        "Cache-Control: no-store",
        "API Capture",
    ):
        assert clause in normalized


def test_api_guide_capture_example_marks_partial_rows_and_all_preview_fields() -> None:
    """The guide distinguishes optional summaries from always-emitted previews."""

    section = _provider_capture_section(
        GUIDE.read_text(encoding="utf-8"),
        "## Provider API Capture",
    )

    assert "以下 collection 行是节选" in section
    for field in (
        '"head"',
        '"tail"',
        '"truncated"',
        '"omitted_bytes"',
        '"bytes"',
        '"characters"',
        '"input_characters"',
        '"output_characters"',
    ):
        assert field in section
    assert "summary 字段按记录内容可选" in section


def test_api_guide_capture_lifecycle_is_explicit_and_mutation_proven() -> None:
    """The guide binds each capture page trigger to its required safe behavior."""

    section = _provider_capture_section(
        GUIDE.read_text(encoding="utf-8"),
        "## Provider API Capture",
    )

    assert _frontend_capture_lifecycle(section) == _FRONTEND_CAPTURE_LIFECYCLE
    for scenario, trigger, behavior in _FRONTEND_CAPTURE_LIFECYCLE:
        row = f"| {scenario} | `{trigger}` | `{behavior}` |"
        assert row in section
        mutant = section.replace(row, "", 1)
        assert _frontend_capture_lifecycle(mutant) == [
            item for item in _FRONTEND_CAPTURE_LIFECYCLE if item[0] != scenario
        ]


def test_current_architecture_sources_do_not_name_removed_http_adapters() -> None:
    """Active architecture sources describe the versioned routers, not deleted modules."""

    diagrams = tuple(sorted((PROJECT_ROOT / "docs" / "refs").glob("*.drawio")))
    assert diagrams, "architecture diagrams moved or renamed"
    sources = (*diagrams, PROJECT_ROOT / "docs" / "plugin-development-guide.md")
    text = "\n".join(source.read_text(encoding="utf-8") for source in sources)

    assert "ConsoleFacade" not in text
    assert "ConsoleRunBroker" not in text
    assert "Feature routes" not in text
    assert "/api/tools" not in text
    assert "/api/plugins/{plugin_id}" not in text


def test_active_markdown_documents_do_not_describe_retired_flat_endpoints() -> None:
    """Every maintained Markdown document shares the final versioned API boundary."""

    retired_references = {
        document.relative_to(PROJECT_ROOT).as_posix(): _RETIRED_ACTIVE_DOC_PATH.search(
            document.read_text(encoding="utf-8")
        ).group(0)
        for document in _active_markdown_documents()
        if _RETIRED_ACTIVE_DOC_PATH.search(document.read_text(encoding="utf-8"))
    }

    assert retired_references == {}


def test_api_guide_publishes_the_stable_error_taxonomy() -> None:
    """Clients can branch on documented codes emitted by the final error handlers."""

    guide = GUIDE.read_text(encoding="utf-8")

    assert _published_error_codes(guide) == _STABLE_ERROR_CODES
    for code, status in _STABLE_ERROR_CODES.items():
        if code in {
            "run_conflict",
            "idempotency_conflict",
            "session_agent_mismatch",
            "session_input_conflict",
            "model_integrity_conflict",
            "provider_in_use",
            "mcp_already_installed",
            "mcp_owned_by_plugin",
            "mcp_activation_failed",
            "mcp_reload_failed",
            "plugin_activation_failed",
            "plugin_reload_failed",
            "plugin_managed_by_system",
            "model_rollback_failed",
            "provider_host_rejected",
        }:
            continue
        assert _status_code(status) == code
    assert _status_code(405) == "http_error"
