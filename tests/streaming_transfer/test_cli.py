"""Agent-native safety coverage for the META-05 CLI.

- if live mode lacks an explicit risk acknowledgement then provider IO is broken
"""

from __future__ import annotations

import pytest

from apps.streaming_transfer.cli import EXIT_SAFETY, main


def test_live_transfer_refuses_before_reading_credentials(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(
        [
            "spotify-to-soundcloud",
            "ABCDEFGHIJKLMNOPQRSTUV",
            "--confidence-threshold",
            "0.70",
            "--output",
            "transfer-report.json",
            "--sharing",
            "private",
            "--live",
        ]
    )

    assert result == EXIT_SAFETY
    assert "--live requires --i-understand-the-risks" in capsys.readouterr().err
