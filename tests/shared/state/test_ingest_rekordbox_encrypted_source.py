"""Fresh-checkout regression: ``ingest-rb`` against the ENCRYPTED working copy.

``paths.copy_live_dbs()`` writes ``data/master.db.copy`` as a byte-for-byte
copy of the live Rekordbox ``master.db``, which is SQLCipher-encrypted. The
README sequence (``cli init`` then ``cli ingest-rb`` with no ``--rb-db``)
therefore used to die on a fresh checkout with::

    sqlite3.DatabaseError: file is not a database

because ``ingest_rb`` opens its source with ``unlock=False``. ``ingest-rb``
now decrypts the working copy to ``data/master.plain.db`` first and ingests
that, which is the decrypted working copy this repo already treats as
canonical.

Everything here is hermetic: the encrypted source is built at test time by
re-encrypting the committed plain fixture with pyrekordbox's own key, so the
production unlock path runs unmodified and no live Rekordbox install is
touched.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from apps.shared import paths as shared_paths
from apps.shared import rekordbox_db
from apps.shared.state import cli as state_cli

pytestmark = [
    pytest.mark.requirement("INFRA-01"),
    pytest.mark.requirement("OPEN-01b"),
]

_MARKER_TABLE = "ingest_rb_plain_copy_marker"


def _pyrekordbox_key() -> str:
    """The key pyrekordbox itself uses when no explicit key is supplied."""
    from pyrekordbox.db6.database import BLOB
    from pyrekordbox.utils import deobfuscate

    return deobfuscate(BLOB)


def _encrypt(plain_src: Path, encrypted_out: Path, key: str) -> None:
    """SQLCipher-encrypt ``plain_src`` into ``encrypted_out`` under ``key``."""
    import sqlcipher3

    con = sqlcipher3.connect(str(plain_src))
    try:
        con.execute(
            "ATTACH DATABASE ? AS encrypted KEY ?", (str(encrypted_out), key)
        )
        con.execute("SELECT sqlcipher_export('encrypted')").fetchone()
        con.commit()
        con.execute("DETACH DATABASE encrypted")
    finally:
        con.close()


def _mark(db_path: Path) -> None:
    """Stamp a sentinel table so a re-decrypt is detectable."""
    con = sqlite3.connect(db_path)
    try:
        con.execute(f"CREATE TABLE {_MARKER_TABLE} (v TEXT)")
        con.commit()
    finally:
        con.close()


def _has_marker(db_path: Path) -> bool:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (_MARKER_TABLE,),
        ).fetchone()
        return row is not None
    finally:
        con.close()


@pytest.fixture(scope="module")
def encrypted_master_copy(
    rb_plain_db_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """The committed plain fixture, SQLCipher-encrypted ONCE per module.

    ``sqlcipher_export`` of the whole fixture is 20-30 s on a CI runner, and it
    was paid inside every test's setup (five times per shard, measured Fri 4
    Sep 2026 on trunk e9ed11ecf). The encrypted bytes are the same every time,
    so they are built once here and COPIED into each test's own data dir
    below; tests that unlink or overwrite their copy touch only that copy.
    The decrypt under test still runs for real, per test, on the production
    path.

    Only tests that DECRYPT take this fixture. The two that discard the copy
    before touching it (unlink it, or overwrite it with bytes that are not a
    cipher file) take ``checkout_layout`` instead: the export is pure cost to
    them, and it is what put them over the fast tier's 120 s per-test wall
    clock (133 s at setup on a loaded pool host, PR #3651 leg 2 of 4, Tue 22
    Sep 2026) although the ledger records them at 0.2 s and 0.47 s.
    """
    root = tmp_path_factory.mktemp("encrypted-master")
    source_plain = root / "source.plain.db"
    shutil.copy2(rb_plain_db_path, source_plain)
    encrypted = root / "master.db.copy"
    _encrypt(source_plain, encrypted, _pyrekordbox_key())
    return encrypted


@pytest.fixture
def checkout_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """A fresh checkout's data dir and paths, with NO ``master.db.copy`` yet.

    The ``encrypted`` path is where ``copy_live_dbs()`` would write the
    working copy; nothing is there until the test (or ``fresh_checkout``)
    puts something there.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    encrypted = data_dir / "master.db.copy"
    plain = data_dir / "master.plain.db"

    monkeypatch.setattr(shared_paths, "REKORDBOX_WORKING_DB", encrypted)
    monkeypatch.setattr(shared_paths, "REKORDBOX_PLAIN_DB", plain)
    monkeypatch.setattr(shared_paths, "STATE_DIR", data_dir / "state")
    # No live DB in a hermetic run, so the staleness warning stays quiet.
    monkeypatch.setattr(
        shared_paths, "REKORDBOX_LIVE_DB", tmp_path / "no-such-live" / "master.db"
    )
    return {
        "data_dir": data_dir,
        "encrypted": encrypted,
        "plain": plain,
        "state_db": tmp_path / "state.db",
    }


@pytest.fixture
def fresh_checkout(
    encrypted_master_copy: Path, checkout_layout: dict[str, Path]
) -> dict[str, Path]:
    """A data dir holding only an ENCRYPTED ``master.db.copy`` -- no plain copy.

    This is exactly the post-``copy_live_dbs()`` state of a fresh checkout.
    """
    shutil.copy2(encrypted_master_copy, checkout_layout["encrypted"])
    return checkout_layout


def test_encrypted_working_copy_is_unreadable_as_plain_sqlite(
    fresh_checkout: dict[str, Path],
) -> None:
    """Guard the premise: the fixture really is SQLCipher-encrypted."""
    con = sqlite3.connect(fresh_checkout["encrypted"])
    try:
        with pytest.raises(sqlite3.DatabaseError, match="file is not a database"):
            con.execute("SELECT COUNT(*) FROM djmdContent").fetchone()
    finally:
        con.close()


def test_readme_sequence_ingests_from_encrypted_working_copy(
    fresh_checkout: dict[str, Path], capsys: pytest.CaptureFixture
) -> None:
    """`init` then `ingest-rb` with no override must work on a fresh checkout.

    Pre-fix this returned 1 with ``sqlite3.DatabaseError: file is not a
    database``, because the default source was the encrypted working copy.
    """
    state_db = fresh_checkout["state_db"]
    assert state_cli.main(["--db", str(state_db), "init"]) == 0
    capsys.readouterr()

    rc = state_cli.main(["--db", str(state_db), "ingest-rb", "--write"])
    captured = capsys.readouterr()
    assert rc == 0, f"ingest-rb failed:\n{captured.out}\n{captured.err}"
    assert "file is not a database" not in captured.err

    assert fresh_checkout["plain"].exists(), "decrypted copy was never produced"
    assert str(fresh_checkout["plain"]) in captured.out

    con = sqlite3.connect(state_db)
    try:
        tracks = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    finally:
        con.close()
    assert tracks > 0, "ingest wrote no tracks"


def test_existing_plain_copy_is_reused_not_redecrypted(
    fresh_checkout: dict[str, Path], capsys: pytest.CaptureFixture
) -> None:
    """Documented convention: the plain copy is static, so reuse it."""
    state_db = fresh_checkout["state_db"]
    assert state_cli.main(["--db", str(state_db), "init"]) == 0
    assert state_cli.main(["--db", str(state_db), "ingest-rb"]) == 0
    capsys.readouterr()

    _mark(fresh_checkout["plain"])
    assert state_cli.main(["--db", str(state_db), "ingest-rb"]) == 0
    out = capsys.readouterr().out
    assert _has_marker(fresh_checkout["plain"]), "plain copy was re-decrypted"
    assert "reusing" in out.lower()


def test_refresh_decrypt_rebuilds_the_plain_copy(
    fresh_checkout: dict[str, Path], capsys: pytest.CaptureFixture
) -> None:
    """``--refresh-decrypt`` is the documented escape hatch from that reuse."""
    state_db = fresh_checkout["state_db"]
    assert state_cli.main(["--db", str(state_db), "init"]) == 0
    assert state_cli.main(["--db", str(state_db), "ingest-rb"]) == 0
    _mark(fresh_checkout["plain"])
    capsys.readouterr()

    assert (
        state_cli.main(["--db", str(state_db), "ingest-rb", "--refresh-decrypt"]) == 0
    )
    out = capsys.readouterr().out
    assert not _has_marker(fresh_checkout["plain"]), "plain copy was not rebuilt"
    assert "decrypted" in out.lower()


def test_missing_working_copy_names_the_step_that_produces_it(
    checkout_layout: dict[str, Path], capsys: pytest.CaptureFixture
) -> None:
    """Fail fast, and say which command populates ``data/master.db.copy``."""
    fresh_checkout = checkout_layout
    assert not fresh_checkout["encrypted"].exists()
    state_db = fresh_checkout["state_db"]
    assert state_cli.main(["--db", str(state_db), "init"]) == 0
    capsys.readouterr()

    rc = state_cli.main(["--db", str(state_db), "ingest-rb"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "master.db.copy" in err
    assert "apps.audit.rekordbox_vs_music" in err
    assert not fresh_checkout["plain"].exists()


def test_undecryptable_source_reports_decryption_failure(
    checkout_layout: dict[str, Path], capsys: pytest.CaptureFixture
) -> None:
    """A bad key / corrupt cipher file must say so, not ingest partially."""
    fresh_checkout = checkout_layout
    fresh_checkout["encrypted"].write_bytes(b"not a sqlcipher database at all")
    state_db = fresh_checkout["state_db"]
    assert state_cli.main(["--db", str(state_db), "init"]) == 0
    capsys.readouterr()

    rc = state_cli.main(["--db", str(state_db), "ingest-rb"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "decrypt" in err.lower()
    assert not fresh_checkout["plain"].exists(), "left a partial plain copy behind"


def test_explicit_rb_db_override_skips_the_decrypt_step(
    fresh_checkout: dict[str, Path],
    rb_plain_db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """``--rb-db`` still means "use exactly this file", plain and unlocked."""
    override = tmp_path / "override.plain.db"
    shutil.copy2(rb_plain_db_path, override)
    state_db = fresh_checkout["state_db"]
    assert state_cli.main(["--db", str(state_db), "init"]) == 0
    capsys.readouterr()

    rc = state_cli.main(
        ["--db", str(state_db), "ingest-rb", "--rb-db", str(override)]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert str(override) in out
    assert not fresh_checkout["plain"].exists()


def test_ensure_plain_db_reports_whether_it_decrypted(
    fresh_checkout: dict[str, Path],
) -> None:
    """The shared helper is honest about doing work vs reusing."""
    plain, decrypted = rekordbox_db.ensure_plain_db()
    assert plain == fresh_checkout["plain"]
    assert decrypted is True

    plain_again, decrypted_again = rekordbox_db.ensure_plain_db()
    assert plain_again == plain
    assert decrypted_again is False

    _, forced = rekordbox_db.ensure_plain_db(refresh=True)
    assert forced is True
