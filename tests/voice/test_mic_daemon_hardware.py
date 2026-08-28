"""Mac-local hardware gate for issue #204.

Run only with every real dependency active:

```
VOICE_MIC_HARDWARE_TEST=1 uv run pytest \
  tests/voice/test_mic_daemon_hardware.py -q -s
```

This test never substitutes a microphone, wake model, VAD, whisper server, or
voice probe endpoint. A pass proves preflight and microphone permission only.
Speaking a wake phrase and comparing the visible browser result with typed
command entry remains the explicit headed Mac acceptance step.
"""

from __future__ import annotations

import json
import os

import pytest

from apps.voice import mic_daemon

pytestmark = [
    pytest.mark.requirement("VOICE-01"),
    pytest.mark.audio_hw,
    pytest.mark.skipif(
        os.environ.get("VOICE_MIC_HARDWARE_TEST") != "1",
        reason="set VOICE_MIC_HARDWARE_TEST=1 on the Mac release host",
    ),
]


def test_real_mic_daemon_preflight(capsys: pytest.CaptureFixture[str]) -> None:
    return_code = mic_daemon.main(["check"])
    payloads = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert return_code == 0
    assert [payload["phase"] for payload in payloads] == [
        "starting",
        "ready",
        "stopped",
    ]
    assert payloads[1]["device"]["max_input_channels"] >= 1
    assert payloads[1]["stt_backend"] == "whisper_cpp"
