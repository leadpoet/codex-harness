"""Attempt-local authenticated HTTP boundary for common sourcing tools."""

from __future__ import annotations

import json
import secrets
import threading
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .providers import LiveProviderTools


class ToolServer(AbstractContextManager["ToolServer"]):
    def __init__(self, providers: LiveProviderTools, token: str | None = None) -> None:
        self.providers = providers
        self.token = token or secrets.token_urlsafe(32)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "LeadpoetBakeoff/1.0"

            def log_message(self, *_: Any) -> None:
                return

            def _reply(self, status: int, body: dict[str, Any]) -> None:
                encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                if self.path != "/tool":
                    self._reply(404, {"ok": False, "error": "not found"})
                    return
                if self.headers.get("Authorization") != f"Bearer {owner.token}":
                    self._reply(401, {"ok": False, "error": "unauthorized"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if size <= 0 or size > 1_000_000:
                        raise ValueError("invalid request size")
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("arguments", {}), dict
                    ):
                        raise ValueError("invalid tool request")
                    result = owner.providers.execute(
                        str(payload.get("name") or ""), payload.get("arguments", {})
                    )
                    self._reply(200, {"ok": True, "result": result})
                except Exception as exc:
                    self._reply(
                        400, {"ok": False, "error": owner.providers.public_error(exc)}
                    )

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "ToolServer":
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
