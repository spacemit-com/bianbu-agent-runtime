#!/usr/bin/env python3
"""Create or update one Bianbu expert through the running Runtime.

The Runtime owns expert identity: ``POST /api/v1/agents`` writes
``agents/<ID>/agent.yaml``, generates the ``com.bianbu.<hex>`` ID, registers the
expert with Security and activates its Worker.  Nothing here writes the data
directory directly, and no credential is ever sent to or returned by the API.

Creating sends the whole declaration.  Updating is a real partial update: only
the fields named on the command line are sent, so an edit to the ``soul`` never
silently drops the Avatar or the capabilities the user did not mention.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:9899"
DEFAULT_AVATAR = "🤖"
_BASE_URL_ENV = "BIANBU_RUNTIME_BASE_URL"
_TIMEOUT_SECONDS = 60.0


def _base_url(value: str) -> str:
    resolved = str(value or "").strip() or os.environ.get(_BASE_URL_ENV, "").strip()
    return (resolved or DEFAULT_BASE_URL).rstrip("/")


def _selectors(values: list[str] | None, field: str) -> tuple[str, ...] | None:
    if values is None:
        return None
    normalized = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
    if "*" in normalized and normalized != ("*",):
        raise ValueError(f"{field} wildcard must be the only entry")
    return normalized


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="",
        help=f"Runtime HTTP base URL（默认 {_BASE_URL_ENV}，再默认 {DEFAULT_BASE_URL}）",
    )
    parser.add_argument(
        "--update",
        default="",
        metavar="EXPERT_ID",
        help="改写已安装专家的声明（PATCH，只发送点名的字段）；不给则新建",
    )
    parser.add_argument("--name", default=None, help="调用名（新建必填）")
    parser.add_argument("--name-zh", default=None, help="中文展示名")
    parser.add_argument("--name-en", default=None, help="英文展示名")
    parser.add_argument("--description", default=None, help="用户可见的一句话摘要")
    parser.add_argument("--description-zh", default=None, help="中文一句话摘要")
    parser.add_argument("--soul", default=None, help="专家行为与边界（新建必填）")
    parser.add_argument("--provider", default=None, help="新建必填；更新时与 --model 成对")
    parser.add_argument("--model", default=None, help="新建必填；更新时与 --provider 成对")
    parser.add_argument("--tool", action="append", default=None)
    parser.add_argument("--skill", action="append", default=None)
    parser.add_argument("--mcp", action="append", default=None)
    parser.add_argument("--plugin", action="append", default=None)
    parser.add_argument(
        "--avatar",
        default=None,
        help=f"emoji 头像（新建默认 {DEFAULT_AVATAR}；更新时只有给出才改）",
    )
    parser.add_argument(
        "--allow-duplicate-name",
        action="store_true",
        help="允许与已安装专家重名（默认拒绝）",
    )
    return parser


def _request(base_url: str, method: str, path: str, payload: dict | None = None) -> object:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(detail).get("detail", detail)
        except ValueError:
            parsed = detail
        if isinstance(parsed, dict):
            parsed = parsed.get("message") or json.dumps(parsed, ensure_ascii=False)
        raise RuntimeError(f"Runtime 拒绝请求（HTTP {exc.code}）：{parsed}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连不上 Runtime（{base_url}）：{exc.reason}") from exc
    if not raw.strip():
        return {}
    return json.loads(raw)


def _agents(base_url: str) -> list[dict]:
    payload = _request(base_url, "GET", "/api/v1/agents")
    data = payload.get("data") if isinstance(payload, dict) else payload
    return [item for item in (data or []) if isinstance(item, dict)]


def _same_name(agents: list[dict], name: str, name_zh: str, name_en: str = "") -> list[dict]:
    wanted = {value.strip() for value in (name, name_zh, name_en) if value.strip()}
    return [
        agent for agent in agents
        if wanted & {
            str(agent.get(field, "")).strip()
            for field in ("name", "name_zh", "name_en")
        }
    ]


def _payload(arguments: argparse.Namespace) -> dict:
    """新建载荷：声明里每个字段都要给全。"""

    name = (arguments.name or "").strip()
    soul = (arguments.soul or "").strip()
    if not name or not soul:
        raise ValueError("新建专家需要 --name 和 --soul")
    return {
        "name": name,
        "name_zh": (arguments.name_zh or "").strip(),
        "name_en": (arguments.name_en or "").strip(),
        "description": (arguments.description or "").strip(),
        "description_zh": (arguments.description_zh or "").strip(),
        "soul": soul,
        "model": _model(arguments, required=True),
        "tools": list(_selectors(arguments.tool, "tools") or ()),
        "skills": list(_selectors(arguments.skill, "skills") or ()),
        "mcps": list(_selectors(arguments.mcp, "mcps") or ()),
        "plugins": list(_selectors(arguments.plugin, "plugins") or ()),
        "metadata": {"avatar": (arguments.avatar or "").strip() or DEFAULT_AVATAR},
    }


def _model(arguments: argparse.Namespace, *, required: bool) -> dict | None:
    provider = (arguments.provider or "").strip()
    model = (arguments.model or "").strip()
    if not provider and not model:
        if required:
            raise ValueError("新建专家需要 --provider 和 --model")
        return None
    if not provider or not model:
        raise ValueError("--provider 和 --model 必须成对给出")
    return {"provider": provider, "name": model}


def _update_payload(arguments: argparse.Namespace) -> dict:
    """更新载荷：只带用户在命令行上点名的字段，其余留给 Runtime 里的现值。"""

    payload: dict = {}
    for field, value in (
        ("name", arguments.name),
        ("name_zh", arguments.name_zh),
        ("name_en", arguments.name_en),
        ("description", arguments.description),
        ("description_zh", arguments.description_zh),
        ("soul", arguments.soul),
    ):
        if value is None:
            continue
        stripped = value.strip()
        if field in {"name", "soul"} and not stripped:
            raise ValueError(f"--{field.replace('_', '-')} 不能为空")
        payload[field] = stripped
    model = _model(arguments, required=False)
    if model is not None:
        payload["model"] = model
    for values, field in (
        (arguments.tool, "tools"),
        (arguments.skill, "skills"),
        (arguments.mcp, "mcps"),
        (arguments.plugin, "plugins"),
    ):
        selectors = _selectors(values, field)
        if selectors is not None:
            payload[field] = list(selectors)
    if arguments.avatar is not None:
        payload["metadata"] = {"avatar": arguments.avatar.strip() or DEFAULT_AVATAR}
    if not payload:
        raise ValueError("--update 至少要给出一个要修改的字段")
    return payload


def _agent(base_url: str, expert_id: str) -> dict:
    payload = _request(base_url, "GET", f"/api/v1/agents/{expert_id}")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError(f"Runtime 里没有这个专家：{expert_id}")
    return data


def _summary(agent: dict) -> dict:
    return {
        "expert_id": str(agent.get("id") or ""),
        "name": str(agent.get("name") or ""),
        "available": bool(agent.get("available")),
        "unavailable_reason": str(agent.get("unavailable_reason") or ""),
        "missing_powers": list(agent.get("missing_powers") or []),
    }


def run(base_url: str, arguments: argparse.Namespace) -> dict:
    if arguments.update:
        expert_id = arguments.update.strip()
        payload = _update_payload(arguments)
        if "metadata" in payload:
            # PATCH 整体替换 metadata，先把现值读回来，别丢掉没点名的键。
            metadata = dict(_agent(base_url, expert_id).get("metadata") or {})
            metadata.update(payload["metadata"])
            payload["metadata"] = metadata
        if not arguments.allow_duplicate_name and {"name", "name_zh", "name_en"} & set(payload):
            duplicates = [
                agent for agent in _same_name(
                    _agents(base_url),
                    str(payload.get("name") or ""),
                    str(payload.get("name_zh") or ""),
                    str(payload.get("name_en") or ""),
                )
                if str(agent.get("id")) != expert_id
            ]
            if duplicates:
                existing = ", ".join(str(agent.get("id")) for agent in duplicates)
                raise RuntimeError(f"已有同名专家（{existing}）：换个名字")
        result = _request(base_url, "PATCH", f"/api/v1/agents/{expert_id}", payload)
    else:
        payload = _payload(arguments)
        if not arguments.allow_duplicate_name:
            duplicates = _same_name(
                _agents(base_url),
                payload["name"],
                payload["name_zh"],
                payload["name_en"],
            )
            if duplicates:
                existing = ", ".join(str(agent.get("id")) for agent in duplicates)
                raise RuntimeError(
                    f"已有同名专家（{existing}）：改名，或确认替换后用 --update <EXPERT_ID>"
                )
        result = _request(base_url, "POST", "/api/v1/agents", payload)
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError(f"Runtime 返回了非预期响应：{json.dumps(result, ensure_ascii=False)[:400]}")
    return _summary(data)


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        summary = run(_base_url(arguments.base_url), arguments)
    except (RuntimeError, OSError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
