"""Regression tests for codex CONFIRMED-FOLLOWUP findings, group D.

Covers:
* P13-F02 -- :func:`apps.dj_copilot.session_context.load_session_context`
  preserves the explicit ``source=...`` label on empty results instead
  of relabelling to ``"empty"``.
* P14-F03 -- ``python -m apps.voice run`` honours ``--enable-destructive``
  and ``--dry-bus`` (reflected in stdout + VoiceContext.destructive).
* P16-F03 -- the conformance adapter loader only swallows
  ``ModuleNotFoundError``; other exceptions propagate.
* P17-02 -- Rust source probe: frecency decay uses ``max(last_played,
  last_dragged)`` rather than ``last_dragged.or(last_played)``.
* P17-03 -- ``apps/launcher/src/hooks/useSearch.ts`` logs the backend
  error before falling back to the in-memory cache.
* F18-03 -- Rust source probe: the Rekordbox dispatch treats
  ``RekordboxMajor::Unknown`` identically to ``Seven`` (clipboard
  fallback) so a closed RB7 install does not degrade to the XML
  sidecar path.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest import mock

import pytest

from apps.dj_copilot.session_context import (
    SessionContext,
    load_session_context,
)


pytestmark = [
    pytest.mark.requirement("AI-01"),
    pytest.mark.requirement("VOICE-02"),
    pytest.mark.requirement("OPEN-03c"),
    pytest.mark.requirement("LAUNCH-02"),
]


REPO_ROOT = Path(__file__).resolve().parents[1]


# -- P13-F02 --------------------------------------------------------------


def test_p13_f02_explicit_source_preserved_on_empty() -> None:
    ctx = load_session_context(source="phase12", conn=None)
    assert ctx.recent == []
    assert ctx.source == "phase12", (
        f"explicit source must be preserved on empty fallback; got {ctx.source!r}"
    )


def test_p13_f02_auto_still_falls_through_to_empty() -> None:
    ctx = load_session_context(source="auto", conn=None)
    assert ctx.source == "empty"


# -- P14-F03 --------------------------------------------------------------


def test_p14_f03_run_honours_enable_destructive_and_dry_bus(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from apps.voice.__main__ import build_parser, _cmd_run

    args = build_parser().parse_args(
        ["run", "--enable-destructive", "--dry-bus"]
    )
    rc = _cmd_run(args)
    # P14-F02 upgraded _cmd_run to a live daemon loop; on a CI runner
    # without sounddevice the daemon bails with rc=2 after printing
    # the resolved-context summary. Contract here is flag plumbing.
    assert rc in (0, 2)
    out = capsys.readouterr().out
    assert "destructive=True" in out, (
        f"--enable-destructive must flip VoiceContext.destructive; stdout={out!r}"
    )
    assert "dry_bus=True" in out, (
        f"--dry-bus must surface in the run summary; stdout={out!r}"
    )


# -- P16-F03 --------------------------------------------------------------


def test_p16_f03_conformance_loader_only_swallows_module_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real adapter exceptions must propagate; only ModuleNotFoundError is
    silently skipped.
    """
    from tests import test_conformance as tc

    # Inject a fake ``apps.adapters.serato`` that raises ValueError on
    # construction. The loader must NOT swallow this.
    class _Boom:
        def __init__(self, *a, **kw):
            raise ValueError("real adapter regression")

    fake_module = mock.MagicMock()
    fake_module.SeratoAdapter = _Boom
    fake_module.SeratoAdapterOptions = lambda **kw: None

    monkeypatch.setitem(sys.modules, "apps.adapters.serato", fake_module)
    with pytest.raises(ValueError, match="real adapter regression"):
        tc._load_adapter("serato")


# -- P17-02 / P17-03 / F18-03: Rust + TS source probes --------------------


def test_p17_02_frecency_decay_uses_max_timestamp() -> None:
    text = (
        REPO_ROOT / "apps/launcher/src-tauri/src/commands/frecency.rs"
    ).read_text(encoding="utf-8")
    # Strip comment lines before scanning so the retrospective reference
    # to the old expression in the fix's doc-comment doesn't trip the guard.
    live = "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("//")
    )
    assert "last_dragged.or(last_played)" not in live, (
        "frecency decay must use the most-recent of the two timestamps"
    )
    assert "p.max(d)" in live


def test_p17_03_use_search_logs_backend_failures() -> None:
    text = (
        REPO_ROOT / "apps/launcher/src/hooks/useSearch.ts"
    ).read_text(encoding="utf-8")
    assert "console.error" in text, (
        "backend search failures must be logged, not silently swallowed"
    )


def test_f18_03_rb_unknown_defaults_to_clipboard() -> None:
    text = (
        REPO_ROOT / "apps/launcher/drag-core/src/drag/rekordbox.rs"
    ).read_text(encoding="utf-8")
    assert "RekordboxMajor::Unknown" in text
    # The dispatch must handle Unknown in the clipboard-fallback branch,
    # not leave it falling through to the XML sidecar path.
    assert "Seven || major == RekordboxMajor::Unknown" in text, (
        "F18-03: closed RB (Unknown major) must share the clipboard path "
        "with RB7 until an installation probe lands"
    )
