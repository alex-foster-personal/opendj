"""Does the Sol review lane's local seat pick a live Codex credential instead
of trusting whatever CODEX_HOME the caller's shell happens to export?

The underlying probe hits a real network endpoint (chatgpt.com's usage API),
so every test here stubs `_weekly_pct` rather than touching the network --
these tests assert the SELECTION POLICY (skip a dead candidate, never fall
back to an API key, fail loudly when everything is dead), not that the
network call itself works.
"""

from __future__ import annotations

import pytest

from scripts import codex_home as codex_home_module
from scripts.codex_home import CodexHomeUnavailable, select_codex_home


def _stub_weekly_pct(monkeypatch: pytest.MonkeyPatch, alive: dict[str, int]) -> list[str]:
    """Replace `_weekly_pct` with a stub; homes not in `alive` raise.

    Returns the list of homes actually probed, in call order, so a test can
    assert the function stopped at the first success rather than probing
    every remaining candidate.
    """
    probed: list[str] = []

    def _fake(home: str, timeout_s: int = 20) -> int:
        probed.append(home)
        if home in alive:
            return alive[home]
        raise ValueError(f"{home}: 401 token_expired")

    monkeypatch.setattr(codex_home_module, "_weekly_pct", _fake)
    return probed


def test_a_dead_candidate_is_skipped_for_the_next_live_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE CASE THIS MODULE EXISTS FOR: a stale ~/.codex-seat-a (401) must
    not stop the local seat from reaching a healthy ~/.codex."""
    probed = _stub_weekly_pct(monkeypatch, {"/home/x/.codex": 42})
    home, pct = select_codex_home(("/home/x/.codex-seat-a", "/home/x/.codex"))
    assert (home, pct) == ("/home/x/.codex", 42)
    assert probed == ["/home/x/.codex-seat-a", "/home/x/.codex"]


def test_the_first_authenticated_candidate_wins_without_probing_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy primary seat must never even touch the standby's quota."""
    probed = _stub_weekly_pct(
        monkeypatch, {"/home/x/.codex": 10, "/home/x/.codex-journey": 99}
    )
    home, pct = select_codex_home(("/home/x/.codex", "/home/x/.codex-journey"))
    assert (home, pct) == ("/home/x/.codex", 10)
    assert probed == ["/home/x/.codex"], (
        "the standby seat must not be probed once the primary works"
    )


def test_when_every_candidate_is_dead_the_caller_gets_an_explicit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No silently-chosen bad home, and no fallback to an API key -- a hard
    raise the caller can turn into a coverage MISS."""
    _stub_weekly_pct(monkeypatch, {})
    with pytest.raises(CodexHomeUnavailable, match="no Codex home authenticated"):
        select_codex_home(("/home/x/.codex", "/home/x/.codex-seat-a"))


def test_the_failure_names_every_candidate_tried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The raised message is the debugging surface -- it must carry each
    candidate's own failure, not just "something failed"."""
    _stub_weekly_pct(monkeypatch, {})
    with pytest.raises(CodexHomeUnavailable) as excinfo:
        select_codex_home(("/home/x/.codex", "/home/x/.codex-journey"))
    assert "/home/x/.codex:" in str(excinfo.value)
    assert "/home/x/.codex-journey:" in str(excinfo.value)
