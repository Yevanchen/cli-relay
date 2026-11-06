"""A local stand-in for the webhook and helpers to configure the notifier in tests."""

from __future__ import annotations

import http.server
import json
import socket
import threading
import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

TOKEN = "test-token-not-a-secret-9f2c"


class FakeWebhook:
    """Records every POST. Each request consumes the next behaviour: 'ok' (200), '429' (with
    Retry-After: 1), '500', '401', or 'drop' (read the request, then close without answering, as
    when the response is lost after the receiver may already have started a run)."""

    def __init__(self, behaviours: Optional[List[str]] = None, port: int = 0, retry_after: str = "1") -> None:
        self.requests: List[Dict[str, Any]] = []
        self.behaviours = list(behaviours or [])
        self.lock = threading.Lock()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with outer.lock:
                    outer.requests.append({"body": json.loads(body), "auth": self.headers.get("Authorization"),
                                           "path": self.path})
                    behaviour = outer.behaviours.pop(0) if outer.behaviours else "ok"
                if behaviour == "drop":
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                code = {"ok": 200, "429": 429, "500": 500, "401": 401}[behaviour]
                self.send_response(code)
                if code == 429:
                    self.send_header("Retry-After", retry_after)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def bodies(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [r["body"] for r in self.requests]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeHTTPReceiver:
    """In-process HTTP stand-in for sandbox tests where binding localhost is forbidden.

    A response is bytes, a JSON value, an HTTP status, or a callable of the request record.
    Both connection classes are patched, so no request can accidentally reach the network.
    """

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests = []

    def __enter__(self):
        import contextlib
        from unittest import mock
        from cli_relay import notify

        self.stack = contextlib.ExitStack()

        def connection(*args, **kwargs):
            conn = mock.Mock()
            request = {}

            def send(method, path, *, body, headers):
                request.update(json.loads(body))
                self.requests.append(request)

            def response():
                value = self.responses.pop(0) if self.responses else {}
                value = value(request["cli_relay"]) if callable(value) else value
                if isinstance(value, BaseException):
                    raise value
                resp = mock.Mock(status=value if type(value) is int else 200)
                resp.getheader.return_value = None
                resp.read.return_value = value if isinstance(value, bytes) else json.dumps(value).encode()
                return resp

            conn.request.side_effect = send
            conn.getresponse.side_effect = response
            return conn

        for cls in ("HTTPConnection", "HTTPSConnection"):
            self.stack.enter_context(mock.patch.object(notify.http.client, cls, side_effect=connection))
        self.stack.enter_context(mock.patch.object(notify.transport, "run_http", side_effect=inline_http))
        self.stack.enter_context(mock.patch.object(notify.socket, "getaddrinfo", return_value=[]))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)


def http_config(tmp: Path, url: str, *, source_id: str = "host-a", max_attempts: int = 4, ack_timeout_s: float = 2.0,
                max_wakes: int = 2, outcomes: Optional[List[str]] = None, default_route: Optional[str] = "bot",
                consumer: str = "bot") -> Dict[str, Any]:
    url_file, token_file = tmp / f"url-{source_id}", tmp / f"token-{source_id}"
    url_file.write_text(url + "\n")
    token_file.write_text(TOKEN + "\n")
    cfg: Dict[str, Any] = {
        "source_id": source_id,
        "routes": {"bot": {"consumer": consumer, "chat": "bot chat", "adapter": {
            "type": "http", "url_file": str(url_file), "token_file": str(token_file), "timeout_s": 3}}},
        "retry": {"max_attempts": max_attempts, "base_delay_s": 0.3, "max_delay_s": 1.0},
        "reminder": {"ack_timeout_s": ack_timeout_s, "max_wakes": max_wakes},
    }
    if default_route:
        cfg["default_route"] = default_route
    if outcomes:
        cfg["notify_outcomes"] = outcomes
    return cfg


def write_config(home: Path, cfg: Dict[str, Any]) -> None:
    """Transport tests start after synthetic verification; no real chat is certified here."""
    from unittest import mock
    from cli_relay import notify, routes
    from cli_relay.errors import UsageError
    from cli_relay.ledger import Ledger

    cfg = copy.deepcopy(cfg)
    clean = None
    try:
        clean = notify.validate_config(cfg)
    except UsageError:
        pass  # invalid-config tests need the exact invalid input
    else:
        for name in clean["routes"]:
            cfg["routes"][name]["verification"] = None
    (Path(home) / "notifier.json").write_text(json.dumps(cfg))
    ledger = Ledger(home)
    try:
        migrated = ledger.has_notifier()
    finally:
        ledger.close()
    if migrated and clean is not None:
        # Exercise the actual proof-writing path rather than forging a config receipt.
        with mock.patch.object(notify, "send", return_value={"outcome": "accepted",
                "why": "visible_message_confirmed", "message_id": "synthetic-test-message"}):
            for name in clean["routes"]:
                routes.verify(home, name, env={})


def inline_http(adapter, payload, env):
    """HTTP unit tests run the exchange inline; supervisor has separate fake-clock tests."""
    from cli_relay import notify
    phases = []
    result = notify._http_exchange(adapter, payload, env, phases.append)
    return {**result, "phase": phases[-1] if phases else "starting"}
