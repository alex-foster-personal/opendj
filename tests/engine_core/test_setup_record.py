"""setup.json: absent is first-run, malformed is an error, never a default.

Single-line intent:
  - if an absent file raised then a genuine first run would look broken
  - if a malformed file defaulted then a dismissed wizard silently re-shows,
    or a completed import silently disappears
  - if a write were not atomic then a crash mid-write leaves a file that
    fails to parse forever after
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.engine_core.setup import record


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    target = tmp_path / "data"
    target.mkdir()
    return target


def test_an_absent_record_is_the_first_run_state(data_dir: Path) -> None:
    saved = record.read(data_dir)
    assert saved.dismissed is False
    assert saved.last_import is None
    assert not record.record_path(data_dir).exists()


def test_a_write_round_trips(data_dir: Path) -> None:
    record.write(
        data_dir,
        record.SetupRecord(dismissed=True, last_import={"tracks": 12}),
    )
    saved = record.read(data_dir)
    assert saved.dismissed is True
    assert saved.last_import == {"tracks": 12}


def test_setting_dismissed_keeps_the_import_history(data_dir: Path) -> None:
    record.set_last_import(data_dir, {"tracks": 12})
    record.set_dismissed(data_dir, True)
    saved = record.read(data_dir)
    assert saved.dismissed is True
    assert saved.last_import == {"tracks": 12}


def test_setting_the_import_keeps_the_dismissal(data_dir: Path) -> None:
    record.set_dismissed(data_dir, True)
    record.set_last_import(data_dir, {"tracks": 3})
    saved = record.read(data_dir)
    assert saved.dismissed is True
    assert saved.last_import == {"tracks": 3}


def test_folder_imports_accumulate_watch_roots_independently_of_last_import(
    data_dir: Path,
) -> None:
    record.set_last_import(data_dir, {"kind": "folder", "roots": ["/one"]})
    record.set_last_import(data_dir, {"kind": "rekordbox", "roots": ["/ignored"]})
    record.set_last_import(data_dir, {"kind": "folder", "roots": ["/two", "/one"]})

    saved = record.read(data_dir)

    assert saved.folder_watch_roots == ["/one", "/two"]


@pytest.mark.parametrize(
    "contents",
    [
        "{not json at all",
        '["a list, not an object"]',
        '{"version": 99, "dismissed": false}',
        '{"version": 1, "dismissed": "yes"}',
        '{"version": 1, "dismissed": false, "last_import": 4}',
    ],
)
def test_a_record_we_cannot_trust_raises(
    data_dir: Path, contents: str
) -> None:
    record.record_path(data_dir).write_text(contents, encoding="utf-8")
    with pytest.raises(record.SetupRecordError):
        record.read(data_dir)


def test_the_write_leaves_no_temp_file_behind(data_dir: Path) -> None:
    record.set_dismissed(data_dir, True)
    assert [p.name for p in data_dir.iterdir()] == [record.RECORD_NAME]
