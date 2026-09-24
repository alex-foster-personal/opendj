"""Unit tests for scripts.perf.capture_mode_ratios telemetry probing."""

from __future__ import annotations

import io
import itertools
import json
from unittest.mock import MagicMock, patch

import pytest

from scripts.perf import capture_mode_ratios as cmr


_OMIT = object()


def _response(payload: dict[str, object]) -> MagicMock:
    response = MagicMock()
    response.status = 200
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response
    return response


def _telemetry_body(
    *,
    available: bool = True,
    footprint_mb: float = 512.0,
    cpu_percent: float = 12.5,
    stale: object = False,
) -> dict[str, object]:
    body: dict[str, object] = {
        "available": available,
        "timestamp": "2026-09-24T06:00:00Z",
        "age_seconds": 3.0,
        "stale": stale,
        "totals": {
            "physical_footprint_mb": footprint_mb,
            "cpu_percent": cpu_percent,
        },
        "by_role_mb": {
            "python-engine": footprint_mb,
        },
    }
    if stale is _OMIT:
        del body["stale"]
    return body


@pytest.mark.requirement("PERFMODE-15")
def test_probe_once_uses_frontend_scoped_telemetry_endpoint() -> None:
    """[if] probe runs [then] it targets the frontend origin telemetry URL, [else stop]."""
    frontend = "http://127.0.0.1:5273"
    payload = _telemetry_body()
    response = MagicMock()
    response.status = 200
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response

    with patch("scripts.perf.capture_mode_ratios.urlopen", return_value=response) as urlopen:
        sample = cmr._probe_once(frontend)

    urlopen.assert_called_once()
    request = urlopen.call_args.args[0]
    assert request.full_url == f"{frontend}/api/v1/performance/telemetry/processes"
    assert sample["totals"]["physical_footprint_mb"] == 512.0
    assert sample["totals"]["cpu_percent"] == 12.5


@pytest.mark.requirement("PERFMODE-15")
def test_probe_once_does_not_shell_out_to_opendj_performance_probe() -> None:
    """[if] probe runs [then] it does not invoke opendj_performance_probe, [else stop]."""
    payload = _telemetry_body()
    response = MagicMock()
    response.status = 200
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response

    with (
        patch("scripts.perf.capture_mode_ratios.urlopen", return_value=response),
        patch("scripts.perf.capture_mode_ratios.subprocess.run") as run,
        patch("scripts.perf.capture_mode_ratios.subprocess.Popen") as popen,
    ):
        cmr._probe_once("http://127.0.0.1:8686")

    run.assert_not_called()
    popen.assert_not_called()


@pytest.mark.requirement("PERFMODE-15")
def test_probe_once_raises_when_telemetry_unavailable() -> None:
    """[if] telemetry is unavailable [then] probe raises, [else stop]."""
    payload = _telemetry_body(available=False)
    response = MagicMock()
    response.status = 200
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response

    with (
        patch("scripts.perf.capture_mode_ratios.urlopen", return_value=response),
        pytest.raises(RuntimeError, match="telemetry unavailable"),
    ):
        cmr._probe_once("http://127.0.0.1:5273")


@pytest.mark.requirement("PERFMODE-15")
def test_probe_once_raises_on_non_200_http_status() -> None:
    """[if] telemetry HTTP status is not 200 [then] probe raises, [else stop]."""
    from email.message import Message
    from urllib.error import HTTPError

    error = HTTPError(
        url="http://127.0.0.1:5273/api/v1/performance/telemetry/processes",
        code=503,
        msg="service unavailable",
        hdrs=Message(),
        fp=io.BytesIO(b""),
    )

    with (
        patch("scripts.perf.capture_mode_ratios.urlopen", side_effect=error),
        pytest.raises(RuntimeError, match="telemetry probe failed \\(503\\)"),
    ):
        cmr._probe_once("http://127.0.0.1:5273")


@pytest.mark.requirement("PERFMODE-15")
@pytest.mark.parametrize("stale", [True, None, "false", 0, _OMIT])
def test_probe_once_reports_unknown_for_telemetry_not_marked_fresh(stale: object) -> None:
    """[if] telemetry totals are not affirmatively fresh [then] probe raises UNKNOWN, [else stop].

    The endpoint serves an old native-probe record with `available: true` and
    `stale: true`; only an explicit `stale: false` is a measurement.
    """
    payload = _telemetry_body(stale=stale)

    with (
        patch("scripts.perf.capture_mode_ratios.urlopen", return_value=_response(payload)),
        pytest.raises(RuntimeError, match="telemetry UNKNOWN .*not fresh"),
    ):
        cmr._probe_once("http://127.0.0.1:5273")


@pytest.mark.requirement("PERFMODE-15")
def test_stale_telemetry_never_reaches_a_steady_sample() -> None:
    """[if] the endpoint only has a stale record [then] no footprint/cpu value is produced, [else stop]."""
    payload = _telemetry_body(stale=True, footprint_mb=999.0)

    with (
        patch("scripts.perf.capture_mode_ratios.urlopen", return_value=_response(payload)),
        patch("scripts.perf.capture_mode_ratios.time.sleep"),
        patch(
            "scripts.perf.capture_mode_ratios.time.monotonic",
            side_effect=itertools.count(0.0, float(cmr._PROBE_INTERVAL_S)),
        ),
        pytest.raises(RuntimeError, match="telemetry UNKNOWN"),
    ):
        cmr._sample_steady("http://127.0.0.1:5273", cmr._MIN_SAMPLE_S)


@pytest.mark.requirement("PERFMODE-15")
def test_probe_once_accepts_fresh_telemetry() -> None:
    """[if] telemetry is marked fresh [then] its totals are the sample, [else stop].

    Control for the overshoot direction: a guard that rejects every body
    passes the UNKNOWN tests above and measures nothing.
    """
    payload = _telemetry_body(stale=False, footprint_mb=321.0, cpu_percent=4.5)

    with patch("scripts.perf.capture_mode_ratios.urlopen", return_value=_response(payload)):
        sample = cmr._probe_once("http://127.0.0.1:5273")

    assert sample == {"totals": {"physical_footprint_mb": 321.0, "cpu_percent": 4.5}}
