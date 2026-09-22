"""Transport timeout classification and elapsed-time errors (CSSTATUS-10)."""

from __future__ import annotations

import urllib.error

import pytest

from apps.sync_hub.transport import (
    PUSH_TIMEOUT_S,
    HttpTransport,
    SyncTransportError,
    classify_transport_failure,
)


@pytest.mark.requirement("CSSTATUS-10")
def test_classify_transport_failure_includes_elapsed_for_502() -> None:
    """[if] a 502 transport failure is classified [then] its kind is proxy timeout, [else stop]."""
    classified = classify_transport_failure(
        "POST https://hub:8870/api/v1/sync/push -> HTTP 502:  (after 31.2s)"
    )
    assert classified is not None
    assert classified.kind == "proxy_or_hub_timeout"
    assert classified.elapsed_s == 31.2
    assert "502" in classified.headline


def test_classify_transport_failure_client_timeout() -> None:
    classified = classify_transport_failure(
        "POST https://hub/push client timeout after 120.0s: timed out"
    )
    assert classified is not None
    assert classified.kind == "client_timeout"
    assert classified.elapsed_s == 120.0


def test_http_transport_records_elapsed_on_url_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TimeoutReason:
        def __str__(self) -> str:
            return "timed out"

    def _raise_timeout(*_args, **_kwargs):
        raise urllib.error.URLError(_TimeoutReason())

    monkeypatch.setattr("urllib.request.urlopen", _raise_timeout)
    transport = HttpTransport("http://hub.test", push_timeout_s=PUSH_TIMEOUT_S)
    with pytest.raises(SyncTransportError) as excinfo:
        transport.post("/api/v1/sync/push", {})
    assert "client timeout after" in str(excinfo.value)


def test_push_timeout_default_is_longer_than_hello() -> None:
    assert PUSH_TIMEOUT_S > 30.0
