"""Write-safety rails tests (OPEN-02c).

None of these tests touch a live Serato DB or real audio file.
"""

from __future__ import annotations

import json

import pytest

from apps.adapters.serato import safety


@pytest.mark.requirement("OPEN-02c")
def test_guard_is_noop_when_not_live_write() -> None:
    """When live_write=False we never check pgrep."""
    called = {"n": 0}

    def fake_pgrep() -> int | None:
        called["n"] += 1
        return 12345  # would block a live write

    safety.guard_live_write(live_write=False, pgrep_fn=fake_pgrep)
    assert called["n"] == 0


@pytest.mark.requirement("OPEN-02c")
def test_guard_raises_when_serato_running() -> None:
    def fake_pgrep() -> int | None:
        return 4242

    with pytest.raises(safety.SeratoRunningError):
        safety.guard_live_write(live_write=True, pgrep_fn=fake_pgrep)


@pytest.mark.requirement("OPEN-02c")
def test_guard_passes_when_serato_not_running() -> None:
    safety.guard_live_write(live_write=True, pgrep_fn=lambda: None)


@pytest.mark.requirement("OPEN-02c")
def test_backup_file_creates_copy(tmp_path) -> None:
    src = tmp_path / "database V2"
    src.write_bytes(b"hello world")
    backup_dir = tmp_path / "backups"
    record = safety.backup_file(src, backup_dir)
    assert record.backup_path.exists()
    assert record.backup_path.read_bytes() == b"hello world"
    # sha256 of "hello world"
    assert record.sha256_before == (
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )


@pytest.mark.requirement("OPEN-02c")
def test_reversal_log_appends_ndjson(tmp_path) -> None:
    src = tmp_path / "a.bin"
    src.write_bytes(b"1")
    backup_dir = tmp_path / "bak"
    record = safety.backup_file(src, backup_dir)
    log = tmp_path / "reversal.ndjson"
    safety.append_reversal(log, record, action="write-database-v2")
    safety.append_reversal(log, record, action="write-geob")
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(ln) for ln in lines]
    assert parsed[0]["action"] == "write-database-v2"
    assert parsed[1]["action"] == "write-geob"
    assert parsed[0]["sha256_before"] == record.sha256_before


@pytest.mark.requirement("OPEN-02c")
def test_is_serato_running_defaults_false(monkeypatch) -> None:
    """When pgrep finds nothing, is_serato_running() is False."""
    monkeypatch.setattr(safety, "_pgrep_serato", lambda: None)
    assert safety.is_serato_running() is False
