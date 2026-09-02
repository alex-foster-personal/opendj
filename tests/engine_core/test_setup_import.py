"""The import pipeline: it works end to end, and it fails in five ways.

Everything here runs against the committed rekordbox fixture copied into a
tmp data dir. Nothing points at the real library, and nothing writes to the
repo's own ``data/``.

Single-line intent:
  - if the fixture import stops populating state.db then a fresh install
    never reaches a working library
  - if an encrypted source, an unreachable key and a failed decrypt share a
    code then the operator cannot tell which one to fix
  - if a stage raises and the run still reports counts then a partial import
    is being sold as a finished one
  - if the analysis counts share a denominator then "0 of 44" stops meaning
    anything
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from apps.engine_core.setup import detect, importer, record


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch data dir, with the shared-state CSV artefact pointed at it.

    ``apps.shared.paths.STATE_DIR`` is bound to ``MDT_DATA_DIR`` at IMPORT
    time, and the ingest CLI writes its csv artefact there. The worker sets
    that env before the first import so it is already correct in production;
    a test calling ``run_import`` in-process has to redirect it, or the
    artefact lands in the repo's own data dir.
    """
    target = tmp_path / "data"
    (target / "state").mkdir(parents=True)
    monkeypatch.setattr("apps.shared.paths.STATE_DIR", target / "state")
    return target


@pytest.fixture
def emitted() -> list[tuple[float, str]]:
    return []


def _emit_into(sink: list[tuple[float, str]]):
    def emit(progress: float, message: str) -> None:
        sink.append((progress, message))

    return emit


def _plain_source(data_dir: Path, source: Path) -> Path:
    """Copy the resolved rekordbox fixture DB into ``data_dir``.

    ``source`` is always the ``rb_plain_db_path`` fixture (root
    ``conftest.py``), which resolves through ``tests.fixtures._resolver``,
    fails closed on a missing fixture host, and checksum-verifies -- rather
    than a hard-coded in-repo path (PR #718 review).
    """
    destination = data_dir / detect.PLAIN_COPY_NAME
    shutil.copy2(source, destination)
    return destination


def _encrypted_source(data_dir: Path) -> Path:
    """A file that is definitively not plain SQLite, byte for byte."""
    destination = data_dir / detect.WORKING_COPY_NAME
    destination.write_bytes(b"\x9f\x1cnot-a-sqlite-header" * 64)
    return destination


def _state_track_count(data_dir: Path) -> int:
    state_db = data_dir / "state" / detect.STATE_DB_NAME
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])
    finally:
        conn.close()


# ----- happy path ---------------------------------------------------------
def test_the_fixture_import_populates_state_db(
    data_dir: Path, emitted: list[tuple[float, str]], rb_plain_db_path: Path
) -> None:
    source = _plain_source(data_dir, rb_plain_db_path)
    outcome = importer.run_import(
        data_dir, emit=_emit_into(emitted), source=source
    )
    assert outcome.tracks > 0
    assert outcome.tracks == _state_track_count(data_dir)
    assert outcome.playlists > 0
    assert outcome.source_was_encrypted is False
    assert outcome.ingested_from == str(source)


def test_every_stage_reports_and_progress_never_goes_backwards(
    data_dir: Path, emitted: list[tuple[float, str]], rb_plain_db_path: Path
) -> None:
    """The bar is the only thing the operator watches. It must be monotonic."""
    importer.run_import(
        data_dir,
        emit=_emit_into(emitted),
        source=_plain_source(data_dir, rb_plain_db_path),
    )
    progresses = [progress for progress, _ in emitted]
    assert progresses == sorted(progresses)
    assert progresses[-1] == pytest.approx(1.0)
    messages = " | ".join(message for _, message in emitted)
    for stage in importer.STAGES:
        assert f"{stage}:" in messages, f"stage {stage} never reported"


def test_the_outcome_is_written_to_setup_json(
    data_dir: Path, emitted: list[tuple[float, str]], rb_plain_db_path: Path
) -> None:
    """Survives a restart, and is readable over HTTP rather than from a tab."""
    outcome = importer.run_import(
        data_dir,
        emit=_emit_into(emitted),
        source=_plain_source(data_dir, rb_plain_db_path),
    )
    saved = record.read(data_dir)
    assert saved.last_import is not None
    assert saved.last_import["tracks"] == outcome.tracks
    assert saved.last_import["finished_at"] == outcome.finished_at


def test_analysis_counts_are_three_named_denominators(
    data_dir: Path, emitted: list[tuple[float, str]], rb_plain_db_path: Path
) -> None:
    """resolvable <= with_analysis_path <= rekordbox_linked, always.

    Quoting a resolvable count against the wrong denominator is the exact
    failure the house rule about honest denominators exists to stop.
    """
    outcome = importer.run_import(
        data_dir,
        emit=_emit_into(emitted),
        source=_plain_source(data_dir, rb_plain_db_path),
    )
    assert outcome.analyses_linked <= outcome.analyses_expected
    assert outcome.analyses_expected <= outcome.rekordbox_tracks
    assert outcome.rekordbox_tracks == outcome.tracks


def test_the_import_reads_a_copy_and_leaves_the_source_alone(
    data_dir: Path,
    tmp_path: Path,
    emitted: list[tuple[float, str]],
    rb_plain_db_path: Path,
) -> None:
    """A source outside the data dir is snapshotted, never ingested in place."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    source = elsewhere / "master.db"
    shutil.copy2(rb_plain_db_path, source)
    before = source.read_bytes()

    outcome = importer.run_import(
        data_dir, emit=_emit_into(emitted), source=source
    )

    assert source.read_bytes() == before
    assert outcome.ingested_from == str(data_dir / detect.WORKING_COPY_NAME)


# ----- refusals -----------------------------------------------------------
def test_a_missing_source_is_rekordbox_not_found(
    data_dir: Path, emitted: list[tuple[float, str]]
) -> None:
    with pytest.raises(importer.SetupImportError) as caught:
        importer.run_import(
            data_dir,
            emit=_emit_into(emitted),
            source=data_dir / "no-such-master.db",
        )
    assert caught.value.code == detect.CODE_REKORDBOX_NOT_FOUND


def test_no_rekordbox_at_all_is_rekordbox_not_found(
    data_dir: Path,
    tmp_path: Path,
    emitted: list[tuple[float, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = tmp_path / "no-rekordbox"
    empty.mkdir()
    monkeypatch.setattr(
        "apps.shared.platform_paths.rekordbox_app_dir", lambda: empty
    )
    with pytest.raises(importer.SetupImportError) as caught:
        importer.run_import(data_dir, emit=_emit_into(emitted))
    assert caught.value.code == detect.CODE_REKORDBOX_NOT_FOUND


def test_an_encrypted_source_without_a_key_is_key_unavailable(
    data_dir: Path,
    emitted: list[tuple[float, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        detect, "key_status", lambda: (False, "no sqlcipher driver here")
    )
    with pytest.raises(importer.SetupImportError) as caught:
        importer.run_import(
            data_dir,
            emit=_emit_into(emitted),
            source=_encrypted_source(data_dir),
        )
    assert caught.value.code == detect.CODE_KEY_UNAVAILABLE
    assert "no sqlcipher driver here" in str(caught.value)


def test_an_undecryptable_source_is_decrypt_failed(
    data_dir: Path,
    emitted: list[tuple[float, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real ensure_plain_db failure, not a stubbed one.

    The bytes written are not a SQLCipher database, so the shared routine
    raises RekordboxDecryptError for the reason it exists to raise it, and
    the importer maps that to its own code rather than letting a raw
    RuntimeError reach the job row.
    """
    monkeypatch.setattr(detect, "key_status", lambda: (True, "key present"))
    with pytest.raises(importer.SetupImportError) as caught:
        importer.run_import(
            data_dir,
            emit=_emit_into(emitted),
            source=_encrypted_source(data_dir),
        )
    assert caught.value.code == detect.CODE_DECRYPT_FAILED


def test_a_decrypt_that_lies_about_succeeding_is_still_decrypt_failed(
    data_dir: Path,
    emitted: list[tuple[float, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returning a path is not evidence. The header is."""

    def _pretend(**kwargs: object) -> tuple[Path, bool]:
        destination = Path(str(kwargs["plain"]))
        destination.write_bytes(b"still ciphertext" * 16)
        return destination, True

    monkeypatch.setattr(detect, "key_status", lambda: (True, "key present"))
    monkeypatch.setattr(
        "apps.shared.rekordbox_db.ensure_plain_db", _pretend
    )
    with pytest.raises(importer.SetupImportError) as caught:
        importer.run_import(
            data_dir,
            emit=_emit_into(emitted),
            source=_encrypted_source(data_dir),
        )
    assert caught.value.code == detect.CODE_DECRYPT_FAILED


def test_nothing_is_reported_when_a_stage_refuses(
    data_dir: Path,
    emitted: list[tuple[float, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No partial success: a refused run writes no last_import record."""
    monkeypatch.setattr(detect, "key_status", lambda: (False, "no key"))
    with pytest.raises(importer.SetupImportError):
        importer.run_import(
            data_dir,
            emit=_emit_into(emitted),
            source=_encrypted_source(data_dir),
        )
    assert record.read(data_dir).last_import is None
    assert not (data_dir / "state" / detect.STATE_DB_NAME).exists()


# ----- the shared decrypt contract ----------------------------------------
def test_the_decrypt_stage_calls_the_shared_routine(
    data_dir: Path,
    emitted: list[tuple[float, str]],
    monkeypatch: pytest.MonkeyPatch,
    rb_plain_db_path: Path,
) -> None:
    """Proves delegation, and that the stage reports the DECRYPTED state.

    The stand-in stands in for SQLCipher only. Everything about which paths
    it is handed, and what the wizard is told afterwards, is the real code.
    """
    calls: list[dict[str, object]] = []

    def _fake_ensure(**kwargs: object) -> tuple[Path, bool]:
        calls.append(kwargs)
        destination = Path(str(kwargs["plain"]))
        shutil.copy2(rb_plain_db_path, destination)
        return destination, True

    monkeypatch.setattr(detect, "key_status", lambda: (True, "key present"))
    monkeypatch.setattr("apps.shared.rekordbox_db.ensure_plain_db", _fake_ensure)
    source = _encrypted_source(data_dir)

    outcome = importer.run_import(
        data_dir, emit=_emit_into(emitted), source=source
    )

    assert calls == [
        {
            "encrypted": source,
            "plain": data_dir / detect.PLAIN_COPY_NAME,
            "refresh": False,
        }
    ]
    assert outcome.source_was_encrypted is True
    assert outcome.tracks > 0
    decrypt_messages = [m for _, m in emitted if m.startswith("decrypt:")]
    assert decrypt_messages == [
        f"decrypt: decrypted {source.name} -> {detect.PLAIN_COPY_NAME}"
    ]


def test_a_reused_plain_copy_says_so_rather_than_claiming_a_decrypt(
    data_dir: Path,
    emitted: list[tuple[float, str]],
    monkeypatch: pytest.MonkeyPatch,
    rb_plain_db_path: Path,
) -> None:
    """The wizard shows the same two states the ingest-rb CLI prints."""

    def _fake_ensure(**kwargs: object) -> tuple[Path, bool]:
        destination = Path(str(kwargs["plain"]))
        shutil.copy2(rb_plain_db_path, destination)
        return destination, False

    monkeypatch.setattr(detect, "key_status", lambda: (True, "key present"))
    monkeypatch.setattr("apps.shared.rekordbox_db.ensure_plain_db", _fake_ensure)

    importer.run_import(
        data_dir, emit=_emit_into(emitted), source=_encrypted_source(data_dir)
    )

    decrypt_messages = [m for _, m in emitted if m.startswith("decrypt:")]
    assert decrypt_messages == [
        f"decrypt: reusing decrypted copy at {data_dir / detect.PLAIN_COPY_NAME}"
    ]


def test_refresh_decrypt_goes_back_to_the_encrypted_snapshot(
    data_dir: Path,
    emitted: list[tuple[float, str]],
    monkeypatch: pytest.MonkeyPatch,
    rb_plain_db_path: Path,
) -> None:
    """Otherwise an existing plain copy wins detection and refresh does nothing.

    Both files are present, which is the normal steady state, so this is the
    case the option exists for.
    """
    snapshot = _encrypted_source(data_dir)
    _plain_source(data_dir, rb_plain_db_path)
    calls: list[dict[str, object]] = []

    def _fake_ensure(**kwargs: object) -> tuple[Path, bool]:
        calls.append(kwargs)
        destination = Path(str(kwargs["plain"]))
        shutil.copy2(rb_plain_db_path, destination)
        return destination, True

    monkeypatch.setattr(detect, "key_status", lambda: (True, "key present"))
    monkeypatch.setattr("apps.shared.rekordbox_db.ensure_plain_db", _fake_ensure)

    importer.run_import(
        data_dir, emit=_emit_into(emitted), refresh_decrypt=True
    )

    assert calls == [
        {
            "encrypted": snapshot,
            "plain": data_dir / detect.PLAIN_COPY_NAME,
            "refresh": True,
        }
    ]


# ----- limit --------------------------------------------------------------
def test_limit_caps_the_ingest(
    data_dir: Path, emitted: list[tuple[float, str]], rb_plain_db_path: Path
) -> None:
    outcome = importer.run_import(
        data_dir,
        emit=_emit_into(emitted),
        source=_plain_source(data_dir, rb_plain_db_path),
        limit=5,
    )
    assert 0 < outcome.tracks <= 5
