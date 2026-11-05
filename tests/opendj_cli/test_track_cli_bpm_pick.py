"""``opendj track bpm-pick``: the CLI twin of the BPM hover card's "use the other tempo" (GRID-OCTAVE-02).

Regression one-liners:
  - if `bpm-pick <id> mik` does not PUT {"source": "mik"} to /bpm-pick then broken
  - if `bpm-pick <id> clear` does not DELETE the pick then broken
  - if a malformed pick reaches the engine instead of printing usage then broken

[if] `opendj track bpm-pick` does not PUT or DELETE the pick, or lets a malformed pick through [then] the CLI twin of the BPM hover card is broken, [else stop].

-Claude
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from apps.opendj_cli import api_cli, track_cli

pytestmark = pytest.mark.requirement("GRID-OCTAVE-02")
SID = "a" * 40


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []

    def fake_run(argv: Sequence[str], *, as_json: bool = False) -> int:
        seen.append(list(argv))
        return 0

    monkeypatch.setattr(api_cli, "run", fake_run)
    return seen


def test_pick_and_clear_call_the_bpm_pick_route(calls: list[list[str]]) -> None:
    assert track_cli.run(["bpm-pick", SID, "mik"], as_json=True) == 0
    assert track_cli.run(["bpm-pick", SID, "clear"], as_json=True) == 0
    path = f"/api/v1/tracks/{SID}/bpm-pick"
    assert calls == [["PUT", path, "--json", '{"source": "mik"}'], ["DELETE", path]]


@pytest.mark.parametrize("argv", [["bpm-pick", SID], ["bpm-pick", SID, "rekordbox"], ["bpm-pick"]])
def test_a_malformed_pick_prints_usage(calls: list[list[str]], argv: list[str]) -> None:
    assert track_cli.run(argv, as_json=True) != 0 and calls == []
