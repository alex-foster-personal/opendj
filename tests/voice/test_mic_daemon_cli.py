"""CLI acceptance tests for the mic daemon lifecycle (VOICE-01).

Requirements:
  ? `check` and `run` expose stable agent-readable JSONL.
  ? Missing real dependencies return exit 2 and never emit `ready`.
  → A successful `ready` event requires Mac-local microphone hardware.

Acceptance tests:
  [if] preflight fails [then ⛔️] emit one terminal error and exit non-zero.
  [if] CLI arguments are invalid [then ⛔️] reject them before mic access.
"""

from __future__ import annotations

import json

import pytest

from apps.voice import mic_daemon

pytestmark = pytest.mark.requirement("VOICE-01")


def test_check_emits_json_error_and_returns_two(capsys, monkeypatch) -> None:
    class PortAudioStyleError(Exception):
        pass

    def _fail_preflight(config: mic_daemon.Config) -> mic_daemon.Runtime:
        raise PortAudioStyleError("PortAudio permission denied")

    monkeypatch.setattr(mic_daemon, "preflight", _fail_preflight)

    return_code = mic_daemon.main(["check"])
    lines = capsys.readouterr().out.splitlines()

    assert return_code == 2
    assert [json.loads(line)["phase"] for line in lines] == ["starting", "error"]
    assert "PortAudio permission denied" in json.loads(lines[-1])["message"]


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--frame-ms", "0"],
        ["run", "--speech-start-timeout-ms", "-1"],
        ["run", "--max-utterance-ms", "0"],
        ["run", "--api-url", "not-a-url"],
    ],
)
def test_invalid_config_is_rejected_before_preflight(argv: list[str], capsys) -> None:
    return_code = mic_daemon.main(argv)
    payloads = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert return_code == 2
    assert payloads[-1]["phase"] == "error"
    assert not any(payload["phase"] == "ready" for payload in payloads)
