"""snapshot -> decrypt -> ingest -> analysis, as five reportable stages.

This is the body of the ``setup.import-rekordbox`` job. It is a plain
function so a test can drive it without a subprocess; :mod:`worker` is the
thin shell that turns its ``emit`` callback into the runner's JSON-lines
protocol.

What it does NOT do is decrypt. The encrypted-working-copy defect is owned
by the ``af--ingest-rb-decrypt`` lane, whose shared helper the CLI will call
before ingest. This module names that seam (:data:`SHARED_DECRYPT_ATTR`),
uses it the moment it exists, and until then refuses an encrypted source
with :data:`detect.CODE_DECRYPT_UNAVAILABLE` -- which says what is missing
and where it is coming from, rather than half-importing a library.

A stage that fails raises :class:`SetupImportError` carrying its code. There
is no partial success: a run that could not ingest does not report a track
count it happened to find lying around from a previous attempt.
"""

from __future__ import annotations

import dataclasses
import importlib
import shutil
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.engine_core.setup import detect, record

Emit = Callable[[float, str], None]

#: The integration point with ``af--ingest-rb-decrypt``. When that lane
#: lands its shared helper under this name, the decrypt stage starts using
#: it with no other change here. If it names the helper something else, this
#: constant is the ONE line to edit.
SHARED_DECRYPT_MODULE: str = "apps.shared.state.ingest.rekordbox"
SHARED_DECRYPT_ATTR: str = "decrypt_working_copy"

#: Progress the run has reached when each stage COMPLETES. The ingest owns
#: half the bar because it owns nearly all the wall clock.
STAGE_PROGRESS: dict[str, float] = {
    "detect": 0.05,
    "snapshot": 0.15,
    "decrypt": 0.30,
    "ingest": 0.85,
    "analysis": 1.00,
}
STAGES: tuple[str, ...] = tuple(STAGE_PROGRESS)


class SetupImportError(RuntimeError):
    """A stage refused. ``code`` is one of :data:`detect.CODES`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclasses.dataclass
class ImportOutcome:
    """What the run actually did. Persisted to ``setup.json`` verbatim."""

    started_at: str
    finished_at: str
    source: str
    source_was_encrypted: bool
    ingested_from: str
    tracks: int
    playlists: int
    analyses_linked: int
    analyses_expected: int
    rekordbox_tracks: int
    share_root: str
    rekordbox_was_running: bool

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def run_import(
    data_dir: Path,
    *,
    emit: Emit,
    source: Path | None = None,
    limit: int | None = None,
) -> ImportOutcome:
    """Import rekordbox into ``data_dir``, reporting every stage.

    ``source`` overrides detection (the fixture path tests take). ``limit``
    caps the ingest for smoke runs. Both are surfaced on the HTTP endpoint,
    so an agent drives the identical flow the wizard drives.
    """
    started_at = _now()
    was_running = detect.rekordbox_is_running()

    chosen, encrypted = _stage_detect(data_dir, emit, source)
    working = _stage_snapshot(data_dir, emit, chosen)
    plain = _stage_decrypt(data_dir, emit, working)
    _stage_ingest(data_dir, emit, plain, limit=limit)
    counts, analyses = _stage_analysis(data_dir, emit, plain)

    outcome = ImportOutcome(
        started_at=started_at,
        finished_at=_now(),
        source=str(chosen),
        source_was_encrypted=bool(encrypted),
        ingested_from=str(plain),
        tracks=counts.tracks,
        playlists=counts.playlists,
        analyses_linked=analyses["resolvable"],
        analyses_expected=analyses["with_analysis_path"],
        rekordbox_tracks=analyses["rekordbox_linked"],
        share_root=str(detect.resolve_share_root()),
        rekordbox_was_running=was_running,
    )
    record.set_last_import(data_dir, outcome.to_dict())
    return outcome


# ----- stages -------------------------------------------------------------
def _stage_detect(
    data_dir: Path, emit: Emit, source: Path | None
) -> tuple[Path, bool]:
    """Settle which file this run reads, and whether it needs the key."""
    if source is not None:
        chosen = Path(source)
        if not chosen.is_file():
            raise SetupImportError(
                detect.CODE_REKORDBOX_NOT_FOUND,
                f"the requested rekordbox database {chosen} does not exist",
            )
        encrypted = not detect.is_plain_sqlite(chosen)
    else:
        found = detect.detect_rekordbox(data_dir)
        if found.import_source is None:
            raise SetupImportError(
                detect.CODE_REKORDBOX_NOT_FOUND,
                "no rekordbox database was found: neither "
                f"{found.live_db.path} (the live install) nor a working copy "
                f"in {data_dir}",
            )
        chosen = Path(found.import_source)
        encrypted = bool(found.import_source_encrypted)

    emit(
        STAGE_PROGRESS["detect"],
        f"detect: reading {chosen.name}"
        + (" (encrypted)" if encrypted else " (already plaintext)"),
    )
    return chosen, encrypted


def _stage_snapshot(data_dir: Path, emit: Emit, chosen: Path) -> Path:
    """Copy the live database into ``data_dir``, or reuse a copy that is there.

    The live database is only ever the SOURCE of a copy2. Nothing downstream
    opens it, so rekordbox running alongside is a freshness caveat rather
    than a conflict.
    """
    if chosen.parent.resolve() == data_dir.resolve():
        emit(
            STAGE_PROGRESS["snapshot"],
            f"snapshot: reusing the working copy already at {chosen.name}",
        )
        return chosen

    destination = data_dir / detect.WORKING_COPY_NAME
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(chosen, destination)
    except OSError as exc:
        raise SetupImportError(
            detect.CODE_REKORDBOX_NOT_FOUND,
            f"could not snapshot {chosen} to {destination}: {exc}",
        ) from exc
    emit(
        STAGE_PROGRESS["snapshot"],
        f"snapshot: copied {chosen} to {destination.name}",
    )
    return destination


def _stage_decrypt(data_dir: Path, emit: Emit, working: Path) -> Path:
    """Hand back a plaintext database, or refuse and say who owns the fix.

    The detect stage's encryption verdict is deliberately NOT reused: the
    snapshot may have replaced the very file that verdict described, so the
    header is re-read against what is on disk now.
    """
    if detect.is_plain_sqlite(working):
        emit(
            STAGE_PROGRESS["decrypt"],
            f"decrypt: {working.name} is already plaintext, nothing to do",
        )
        return working

    key_available, key_detail = detect.key_status()
    if not key_available:
        raise SetupImportError(
            detect.CODE_KEY_UNAVAILABLE,
            f"{working} is encrypted and no SQLCipher key is available: "
            f"{key_detail}",
        )

    helper = _shared_decrypt_helper()
    if helper is None:
        raise SetupImportError(
            detect.CODE_DECRYPT_UNAVAILABLE,
            f"{working} is encrypted and this engine has no decrypt step: "
            f"{SHARED_DECRYPT_MODULE}.{SHARED_DECRYPT_ATTR} does not exist "
            "yet. It is owned by the af--ingest-rb-decrypt lane; until that "
            "lands, point the import at an already-decrypted "
            f"{detect.PLAIN_COPY_NAME}.",
        )

    plain = data_dir / detect.PLAIN_COPY_NAME
    try:
        produced = helper(working, plain)
    except Exception as exc:
        raise SetupImportError(
            detect.CODE_DECRYPT_FAILED,
            f"decrypting {working} to {plain} failed: {type(exc).__name__}: "
            f"{exc}",
        ) from exc

    result = Path(produced) if produced is not None else plain
    if not detect.is_plain_sqlite(result):
        raise SetupImportError(
            detect.CODE_DECRYPT_FAILED,
            f"the decrypt step reported success but {result} is still not a "
            "plaintext SQLite file",
        )
    emit(
        STAGE_PROGRESS["decrypt"],
        f"decrypt: wrote {result.name}",
    )
    return result


def _stage_ingest(
    data_dir: Path, emit: Emit, plain: Path, *, limit: int | None
) -> None:
    """Drive the shared-state CLI: ``init`` then ``ingest-rb --write``.

    The CLI is called rather than reimplemented, so the wizard and
    ``python -m apps.shared.state.cli`` cannot drift apart -- and so the
    decrypt fix landing in the CLI lands here for free.
    """
    from apps.shared.state import cli as state_cli

    state_db = data_dir / "state" / detect.STATE_DB_NAME
    emit(STAGE_PROGRESS["decrypt"], f"ingest: preparing {state_db}")
    if state_cli.main(["--db", str(state_db), "init"]) != 0:
        raise SetupImportError(
            detect.CODE_INGEST_FAILED,
            f"could not create or migrate the state database at {state_db}",
        )

    argv = [
        "--db",
        str(state_db),
        "ingest-rb",
        "--rb-db",
        str(plain),
        "--write",
        "--stale-ok",
    ]
    if limit is not None:
        argv += ["--limit", str(limit)]
    emit(0.40, f"ingest: reading {plain.name} into the library")
    code = state_cli.main(argv)
    if code != 0:
        raise SetupImportError(
            detect.CODE_INGEST_FAILED,
            f"`apps.shared.state.cli {' '.join(argv)}` exited {code}; its "
            "output is on the job's stderr",
        )
    emit(STAGE_PROGRESS["ingest"], "ingest: finished")


def _stage_analysis(
    data_dir: Path, emit: Emit, plain: Path
) -> tuple[detect.LibraryCounts, dict[str, int]]:
    """Check the ANLZ analyses the ingested tracks point at are reachable.

    Nothing is copied: waveforms are served straight out of the rekordbox
    share root at read time. What this stage establishes is whether that
    root actually holds the files the freshly ingested rows name -- the
    difference between a library that will draw waveforms and one that will
    silently draw none.

    Denominators are named, never merged: ``rekordbox_linked`` is every row
    the ingest linked to a rekordbox id, ``with_analysis_path`` is the
    subset rekordbox recorded an AnalysisDataPath for, and ``resolvable`` is
    the subset of THAT whose directory is present under the share root.
    """
    counts = detect.library_counts(data_dir)
    analyses = _count_resolvable_analyses(data_dir, plain)
    share_root = detect.resolve_share_root()
    emit(
        STAGE_PROGRESS["analysis"],
        (
            f"analysis: {analyses['resolvable']} of "
            f"{analyses['with_analysis_path']} rekordbox analyses resolve "
            f"under {share_root} "
            f"({analyses['rekordbox_linked']} tracks linked)"
        ),
    )
    return counts, analyses


def _count_resolvable_analyses(data_dir: Path, plain: Path) -> dict[str, int]:
    """Join state.db's rekordbox links against djmdContent.AnalysisDataPath."""
    from apps.shared.platform_paths import resolve_asset_path

    state_db = data_dir / "state" / detect.STATE_DB_NAME
    if not state_db.is_file() or not plain.is_file():
        return {
            "rekordbox_linked": 0,
            "with_analysis_path": 0,
            "resolvable": 0,
        }

    state_conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        vendor_ids = {
            str(row[0])
            for row in state_conn.execute(
                "SELECT vendor_id FROM track_vendor_ids WHERE vendor = ?",
                ("rekordbox",),
            )
        }
    finally:
        state_conn.close()

    rb_conn = sqlite3.connect(f"file:{plain}?mode=ro", uri=True)
    try:
        rows = rb_conn.execute(
            "SELECT ID, AnalysisDataPath FROM djmdContent"
        ).fetchall()
    finally:
        rb_conn.close()

    with_path = 0
    resolvable = 0
    for content_id, analysis_path in rows:
        if str(content_id) not in vendor_ids:
            continue
        if not analysis_path:
            continue
        with_path += 1
        mapped = resolve_asset_path(str(analysis_path))
        if mapped.resolved is not None and mapped.resolved.parent.is_dir():
            resolvable += 1

    return {
        "rekordbox_linked": len(vendor_ids),
        "with_analysis_path": with_path,
        "resolvable": resolvable,
    }


# ----- seams --------------------------------------------------------------
def _shared_decrypt_helper() -> Callable[[Path, Path], Path | None] | None:
    """Resolve the shared decrypt helper, or ``None`` while it is unwritten.

    Expected contract: ``helper(encrypted_src, plain_dst)`` writes a plain
    SQLite copy and returns its path (or ``None``, meaning ``plain_dst``).
    """
    try:
        module = importlib.import_module(SHARED_DECRYPT_MODULE)
    except ImportError:
        return None
    helper = getattr(module, SHARED_DECRYPT_ATTR, None)
    return helper if callable(helper) else None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = [
    "SHARED_DECRYPT_ATTR",
    "SHARED_DECRYPT_MODULE",
    "STAGES",
    "STAGE_PROGRESS",
    "Emit",
    "ImportOutcome",
    "SetupImportError",
    "run_import",
]
