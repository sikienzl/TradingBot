from __future__ import annotations

import json
from urllib.error import URLError
from urllib.request import Request

import pytest

from scripts.alert_webhook_relay import forward_alert


class _Response:
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_forward_alert_posts_json_to_https_receiver():
    seen = {}

    def post(request: Request, timeout: int):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.data)
        seen["timeout"] = timeout
        return _Response()

    forward_alert(b'{"status":"firing"}', "https://hooks.example.test/secret", post=post)

    assert seen["url"] == "https://hooks.example.test/secret"
    assert seen["method"] == "POST"
    assert seen["body"] == {"status": "firing"}
    assert seen["timeout"] == 5


def test_forward_alert_rejects_non_https_url():
    with pytest.raises(ValueError, match="HTTPS"):
        forward_alert(b'{"status":"firing"}', "http://public.example.test/hook")


def test_forward_alert_rejects_invalid_or_oversized_payload():
    with pytest.raises(TypeError, match="JSON object"):
        forward_alert(b"[]", "https://hooks.example.test/secret")
    with pytest.raises(ValueError, match="too large"):
        forward_alert(b" " * (1_000_001), "https://hooks.example.test/secret")


def test_forward_alert_propagates_receiver_failure():
    def fail(_request, timeout: int):
        raise URLError("receiver unavailable")

    with pytest.raises(URLError):
        forward_alert(b'{"status":"firing"}', "https://hooks.example.test/secret", post=fail)
