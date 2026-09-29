"""http_get_json: transient 5xx must not kill a resumable fetch stage.

- if a 503 storm (lrclib, batch-2 AND batch-3a incidents) kills the candidates
  stage on the first hit then whole batch runs die on provider blips -- broken
- if retries never give up then an outage hangs the driver silently -- broken
- if a 4xx is retried then quota/auth mistakes burn the daily budget -- broken
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from apps.lyrics.sources import base
from tests.sleep_spy import record_own_thread_sleeps


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "boom", {}, io.BytesIO(b""))


class _Resp:
    status = 200

    def read(self) -> bytes:
        return json.dumps({"ok": True}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch(monkeypatch: pytest.MonkeyPatch, outcomes: list) -> list[float]:
    # base.time is the process-wide time module: record this thread's sleeps only.
    sleeps = record_own_thread_sleeps(monkeypatch)

    def fake_urlopen(_req, timeout=None):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return sleeps


def test_503_storm_is_retried_with_backoff_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps = _patch(monkeypatch, [_http_error(503), _http_error(503), _Resp()])
    status, body = base.http_get_json("http://x/search", {"q": "a"})
    assert (status, body) == (200, {"ok": True})
    assert sleeps == [2.0, 4.0], "exponential backoff, base 2s"


def test_persistent_503_raises_after_bounded_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps = _patch(monkeypatch, [_http_error(503)] * base.RETRY_ATTEMPTS)
    with pytest.raises(urllib.error.HTTPError):
        base.http_get_json("http://x/search", {"q": "a"})
    assert len(sleeps) == base.RETRY_ATTEMPTS - 1, "gives up loudly, never hangs"


def test_4xx_is_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = _patch(monkeypatch, [_http_error(429)])
    with pytest.raises(urllib.error.HTTPError):
        base.http_get_json("http://x/get", {"q": "a"})
    assert sleeps == []


def test_404_stays_a_soft_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [_http_error(404)])
    assert base.http_get_json("http://x/get", {"q": "a"}) == (404, None)
