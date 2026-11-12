"""``opendj track analysis-range``: the CLI twin of the popover's "Re-analyze grid" (ANALYSIS-11723).

Regression one-liners:
  - if `analysis-range <id> set 93-186` does not PUT {"range": "93-186"} then broken
  - if `clear` does not DELETE, or `show` does not GET, the range then broken
  - if `reanalyze` does not POST the beatgrid order then the CLI cannot re-analyze: broken
  - if a malformed call reaches the engine instead of printing usage then broken

[if] opendj track analysis-range cannot drive the HTTP routes [then] agent parity is broken, [else stop].

-Claude
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from apps.opendj_cli import api_cli, track_cli

pytestmark = pytest.mark.requirement("ANALYSIS-11723")
SID = "a" * 40


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []

    def fake_run(argv: Sequence[str], *, as_json: bool = False) -> int:
        seen.append(list(argv))
        return 0

    monkeypatch.setattr(api_cli, "run", fake_run)
    return seen


def test_set_clear_show_and_reanalyze_drive_the_routes(calls: list[list[str]]) -> None:
    for verb in (["set", "93-186"], ["clear"], ["show"], ["reanalyze"]):
        assert track_cli.run(["analysis-range", SID, *verb], as_json=True) == 0
    path = f"/api/v1/tracks/{SID}/analysis-range"
    assert calls == [
        ["PUT", path, "--json", '{"range": "93-186"}'],
        ["DELETE", path],
        ["GET", path],
        ["POST", f"/api/v1/analysis-queue/orders/{SID}/beatgrid"],
    ]


@pytest.mark.parametrize(
    "argv",
    [["analysis-range"], ["analysis-range", SID], ["analysis-range", SID, "set"], ["analysis-range", SID, "bogus"]],
)
def test_a_malformed_call_prints_usage(calls: list[list[str]], argv: list[str]) -> None:
    assert track_cli.run(argv, as_json=True) != 0 and calls == []
