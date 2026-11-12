"""``opendj track key-edit``: the CLI twin of the library key cell's edit (KEY-10229).

[if] key-edit set clear or show does not hit the HTTP key-edit routes [then] fail, [else stop].

Regression one-liners:
  - if `key-edit <id> set 8A` does not PUT {"key": "8A"} to /key-edit then broken
  - if `key-edit <id> clear` does not DELETE the edit then broken
  - if `key-edit <id> show` (or bare `<id>`) does not GET the edit then broken
  - if a malformed call reaches the engine instead of printing usage then broken

-Claude
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from apps.opendj_cli import api_cli, track_cli

pytestmark = pytest.mark.requirement("KEY-10229")
SID = "a" * 40
PATH = f"/api/v1/tracks/{SID}/key-edit"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []

    def fake_run(argv: Sequence[str], *, as_json: bool = False) -> int:
        seen.append(list(argv))
        return 0

    monkeypatch.setattr(api_cli, "run", fake_run)
    return seen


def test_set_clear_and_show_call_the_key_edit_route(calls: list[list[str]]) -> None:
    for argv in (["set", "8A"], ["clear"], ["show"], []):
        assert track_cli.run(["key-edit", SID, *argv], as_json=True) == 0
    assert calls == [
        ["PUT", PATH, "--json", '{"key": "8A"}'],
        ["DELETE", PATH],
        ["GET", PATH],
        ["GET", PATH],
    ], "if key-edit does not drive PUT/DELETE/GET on /key-edit then broken"


@pytest.mark.parametrize(
    "argv", [["key-edit"], ["key-edit", SID, "set"], ["key-edit", SID, "wipe"], ["key-edit", SID, "set", "8A", "x"]]
)
def test_a_malformed_key_edit_prints_usage(calls: list[list[str]], argv: list[str]) -> None:
    assert track_cli.run(argv, as_json=True) != 0 and calls == [], (
        "if a malformed key-edit reaches the engine then broken"
    )
