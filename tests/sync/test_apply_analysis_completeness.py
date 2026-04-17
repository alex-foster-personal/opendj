"""SYNC-05 regression: ``apply_analysis`` refuses to partial-apply.

Codex P04-02: ``_write_rb_field`` only implements ``bpm`` and
``energy``; for ``manual_bpm`` / ``key_camelot`` / ``tags`` it used to
return ``False`` and the CLI still exited 0, reporting success for a
no-op. This test locks in the non-zero exit behaviour.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from apps.sync import apply_analysis
from apps.sync.apply_analysis import (
    UnsupportedRbFieldError,
    _SUPPORTED_RB_WRITE_FIELDS,
    _write_rb_field,
    live_run,
    main,
)


@pytest.mark.requirement("SYNC-05")
@pytest.mark.parametrize("field", ["manual_bpm", "key_camelot", "tags"])
def test_write_rb_field_refuses_unsupported(field: str) -> None:
    """Unsupported RB-side fields must raise, not silently return False."""
    with pytest.raises(UnsupportedRbFieldError):
        _write_rb_field(db=None, content_id="1", field=field, value="x")


@pytest.mark.requirement("SYNC-05")
def test_supported_fields_are_bpm_and_energy_only() -> None:
    assert _SUPPORTED_RB_WRITE_FIELDS == frozenset({"bpm", "energy"})


@pytest.mark.requirement("SYNC-05")
def test_live_run_raises_on_unsupported_rb_field(tmp_path: Path) -> None:
    rows = [
        {
            "rb_content_id": "1",
            "djay_uuid": "uuid-1",
            "field": "manual_bpm",
            "rb_value": "",
            "djay_value": "128.0",
            "resolution": "accept_djay",
            "action_hint": "write djay -> RB",
        }
    ]
    with pytest.raises(UnsupportedRbFieldError):
        live_run(
            rows,
            fields={"manual_bpm"},
            flag_ok=True,
            rb_db_path=tmp_path / "rb.db",
            djay_db_path=tmp_path / "djay.db",
        )


@pytest.mark.requirement("SYNC-05")
def test_cli_exits_nonzero_on_unsupported_rb_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """``main`` must NOT exit 0 when the plan contains unsupported RB fields.

    This is the exact regression Codex P04-02 described.
    """
    diff_csv = tmp_path / "analysis-diff.csv"
    with diff_csv.open("w", newline="", encoding="utf-8") as fp:
        w = csv.writer(fp)
        w.writerow([
            "rb_content_id", "djay_uuid", "field",
            "rb_value", "djay_value", "resolution", "action_hint",
        ])
        w.writerow([
            "1", "uuid-1", "key_camelot",
            "", "8A", "accept_djay", "write djay -> RB",
        ])

    # Route live-run away from real libraries.
    monkeypatch.setattr(
        apply_analysis, "_live_rb_db_path", lambda live: tmp_path / "rb.db"
    )
    monkeypatch.setattr(
        apply_analysis, "_live_djay_db_path", lambda live: tmp_path / "djay.db"
    )

    rc = main([
        "--diff-csv", str(diff_csv),
        "--live",
        "--i-understand-the-risks",
        "--fields", "key_camelot",
    ])
    assert rc != 0, (
        "apply_analysis must not exit 0 when the RB plan contains an "
        "unsupported field; regression of Codex P04-02."
    )
    err = capsys.readouterr().err
    assert "UnsupportedRbField" in err
