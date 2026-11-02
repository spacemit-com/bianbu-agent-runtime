"""OpenAI-compatible recording provider for the DLP acceptance check.

It records every received request body to JSONL and echoes the last user
message back as the assistant reply.  Echoing matters: the model therefore
answers with the stand-ins it was given, and the Runtime must restore them to
the real values the user sees.
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_LOG = "/tmp/provider_requests.jsonl"


def _reply_for(document: dict) -> str:
    for message in reversed(document.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(
                    str(part.get("text") or "") for part in content
                    if isinstance(part, dict))
    return "OK"


def _completion(reply: str) -> dict:
    return {
        "id": "capture", "object": "chat.completion", "created": 0,
        "model": "capture",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": reply}}],
    }


def _chunks(reply: str) -> list[dict]:
    return [
        {"id": "capture", "object": "chat.completion.chunk", "created": 0,
         "model": "capture",
         "choices": [{"index": 0, "delta": {"content": reply},
                      "finish_reason": None}]},
        {"id": "capture", "object": "chat.completion.chunk", "created": 0,
         "model": "capture",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


class Handler(BaseHTTPRequestHandler):
    log_path = DEFAULT_LOG

    def log_message(self, *args):  # noqa: D401 - silence the default access log
        pass

    def _record(self) -> dict:
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        try:
            document = json.loads(raw)
        except ValueError:
            document = {"raw": raw.decode("utf-8", "replace")}
        with open(self.log_path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"path": self.path, "body": document},
                                    ensure_ascii=False) + "\n")
        return document if isinstance(document, dict) else {}

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        document = self._record()
        reply = _reply_for(document)
        if document.get("stream"):
            body = "".join(
                "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                for chunk in _chunks(reply)
            ) + "data: [DONE]\n\n"
            encoded = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        encoded = json.dumps(_completion(reply)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        encoded = json.dumps({"data": [{"id": "capture"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19900
    if len(sys.argv) > 2:
        Handler.log_path = sys.argv[2]
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
