"""Unit tests for scripts.perf.capture_mode_ratios telemetry probing."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from scripts.perf import capture_mode_ratios as cmr


def _telemetry_body(
    *,
    available: bool = True,
    footprint_mb: float = 512.0,
    cpu_percent: float = 12.5,
) -> dict[str, object]:
    return {
        "available": available,
        "totals": {
            "physical_footprint_mb": footprint_mb,
            "cpu_percent": cpu_percent,
        },
        "by_role_mb": {
            "python-engine": footprint_mb,
        },
    }


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
    from urllib.error import HTTPError

    error = HTTPError(
        url="http://127.0.0.1:5273/api/v1/performance/telemetry/processes",
        code=503,
        msg="service unavailable",
        hdrs=None,
        fp=io.BytesIO(b""),
    )

    with (
        patch("scripts.perf.capture_mode_ratios.urlopen", side_effect=error),
        pytest.raises(RuntimeError, match="telemetry probe failed \\(503\\)"),
    ):
        cmr._probe_once("http://127.0.0.1:5273")
