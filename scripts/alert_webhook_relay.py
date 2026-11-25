#!/usr/bin/env python3
"""Loopback-only Alertmanager webhook relay to a private HTTPS endpoint."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import URLError
from urllib.request import Request, urlopen

MAX_BODY_BYTES = 1_000_000


def forward_alert(payload: bytes, webhook_url: str, *, post=urlopen) -> None:
    if len(payload) > MAX_BODY_BYTES:
        raise ValueError("Alert payload is too large")
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise TypeError("Alert payload must be a JSON object")
    if not webhook_url.startswith("https://"):
        raise ValueError("Alert webhook URL must use HTTPS")
    request = Request(
        webhook_url,
        data=json.dumps(parsed).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with post(request, timeout=5) as response:
        if not 200 <= response.status < 300:
            raise URLError(f"Alert receiver returned HTTP {response.status}")


class AlertRelayHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.client_address[0] not in {"127.0.0.1", "::1"}:
            self.send_error(403)
            return
        if self.path != "/alerts":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                self.send_error(413)
                return
            payload = self.rfile.read(length)
            forward_alert(payload, os.environ["ALERT_WEBHOOK_URL"])
        except (KeyError, OSError, TypeError, ValueError, URLError) as exc:
            self.send_error(502, str(exc))
            return
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    server = HTTPServer(("127.0.0.1", int(os.getenv("ALERT_RELAY_PORT", "9095"))), AlertRelayHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
