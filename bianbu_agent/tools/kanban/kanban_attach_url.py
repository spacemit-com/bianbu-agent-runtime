import asyncio
import base64
import http.client
import ipaddress
import json
import socket
import ssl
import urllib.parse
from pathlib import Path

from bianbu_agent.tools import mutate_kanban, tool

_MAX_BYTES = 25 * 1024 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _resolve_public_url(url):
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("url must use public HTTP(S)")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("url hostname could not be resolved") from exc
    if not addresses or not all(
        ipaddress.ip_address(item[4][0]).is_global for item in addresses
    ):
        raise ValueError("url must resolve only to public addresses")
    return parsed, addresses


def _connected_socket(parsed, addresses, timeout=30):
    last_error = None
    for family, socktype, protocol, _canonical_name, sockaddr in addresses:
        connection = socket.socket(family, socktype, protocol)
        connection.settimeout(timeout)
        try:
            connection.connect(sockaddr)
            if parsed.scheme == "https":
                connection = ssl.create_default_context().wrap_socket(
                    connection, server_hostname=parsed.hostname
                )
            return connection
        except OSError as exc:
            last_error = exc
            connection.close()
    raise ValueError(f"url connection failed: {last_error}")


def _request_once(parsed, addresses):
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection = http.client.HTTPConnection(parsed.hostname, port, timeout=30)
    connection.sock = _connected_socket(parsed, addresses)
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    try:
        connection.request(
            "GET",
            target,
            headers={"Accept-Encoding": "identity", "User-Agent": "bianbu-agent/1"},
        )
        response = connection.getresponse()
        content = response.read(_MAX_BYTES + 1)
        return (
            response.status,
            response.getheader("Location"),
            content,
            response.headers.get_content_type(),
        )
    finally:
        connection.close()


def _download(url):
    current_url = url
    for _redirect in range(6):
        parsed, addresses = _resolve_public_url(current_url)
        status, location, content, content_type = _request_once(parsed, addresses)
        if status in _REDIRECT_STATUSES:
            if not location:
                raise ValueError("redirect response is missing Location")
            current_url = urllib.parse.urljoin(current_url, location)
            continue
        if status >= 400:
            raise ValueError(f"download failed with HTTP status {status}")
        if len(content) > _MAX_BYTES:
            raise ValueError("attachment exceeds 25 MB")
        return content, content_type
    raise ValueError("too many redirects")


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def kanban_attach_url(
        task_id: str,
        url: str,
        board: str = None,
        content_type: str = None,
        filename: str = None,
    ):
        """从安全的公开 HTTP(S) URL 下载文件并附加到 Kanban 任务。

        Args:
            task_id: 要操作的 Kanban 任务 ID（必填）。
            url: http(s) URL to fetch and store.
            board: 可选看板标识；省略时使用默认看板。
            content_type: Optional MIME type override. Defaults to the Content-Type the server returns.
            filename: Optional name to store it under. Defaults to the URL path's leaf component.
        """
        arguments = {
            key: value
            for key, value in {
                "task_id": task_id,
                "url": url,
                "board": board,
                "content_type": content_type,
                "filename": filename,
            }.items()
            if value is not None
        }
        try:
            content, detected_type = await asyncio.to_thread(
                _download, str(arguments.get("url", ""))
            )
        except Exception as exc:
            return json.dumps(
                {"success": False, "code": "download_failed", "error": str(exc)}
            )
        filename = Path(
            str(
                arguments.get("filename")
                or urllib.parse.urlparse(str(arguments.get("url"))).path
                or "attachment"
            )
        ).name

        def attach(state):
            task = state.setdefault("tasks", {}).get(str(arguments.get("task_id", "")))
            if not task:
                return {
                    "success": False,
                    "code": "not_found",
                    "error": "Kanban task not found",
                }
            item = {
                "filename": filename,
                "content_type": arguments.get("content_type") or detected_type,
                "size": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
            task.setdefault("attachments", []).append(item)
            return {
                "attachment": {
                    key: value for key, value in item.items() if key != "content_base64"
                }
            }

        payload = await asyncio.to_thread(
            mutate_kanban,
            package_context.workspace_root,
            arguments.get("board"),
            attach,
        )
        return json.dumps(payload, ensure_ascii=False)

    return kanban_attach_url
