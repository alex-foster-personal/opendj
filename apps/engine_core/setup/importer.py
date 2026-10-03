"""snapshot -> decrypt -> ingest -> analysis, as five reportable stages.

This is the body of the ``setup.import-rekordbox`` job. It is a plain
function so a test can drive it without a subprocess; :mod:`worker` is the
thin shell that turns its ``emit`` callback into the runner's JSON-lines
protocol.

The decrypt is NOT reimplemented here. ``apps.shared.rekordbox_db`` owns the
single ATTACH + ``sqlcipher_export`` routine and its reuse-when-present
policy; this module calls ``ensure_plain_db`` and reports the two states it
distinguishes -- "decrypted X -> Y" and "reusing decrypted copy at Y" -- in
the same words the ``ingest-rb`` CLI prints them. It runs as its own stage
rather than being left implicit inside the CLI for one reason: the wizard
has to be able to SHOW it. The plain path is then handed to ``ingest-rb``
explicitly, so the work is never done twice.

A stage that fails raises :class:`SetupImportError` carrying its code. There
is no partial success: a run that could not ingest does not report a track
count it happened to find lying around from a previous attempt.
"""

from __future__ import annotations

import dataclasses
import shutil
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.engine_core.setup import detect, record
from apps.shared.scan_mass_missing import MassMissingError
from apps.shared.state.ingest.path_collisions import PathCollisionError

Emit = Callable[[float, str], None]

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

#: The folder import has no rekordbox database, so no snapshot and no
#: decrypt. Three stages, and the wizard renders whichever list applies.
FOLDER_STAGE_PROGRESS: dict[str, float] = {
    "detect": 0.05,
    "scan": 0.20,
    "ingest": 1.00,
}
FOLDER_STAGES: tuple[str, ...] = tuple(FOLDER_STAGE_PROGRESS)


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
    #: Folders macOS refused to list during this run. The caveat that has to
    #: travel WITH the counts above: an import that could not read the music
    #: folder still imports every row rekordbox knows about, and every one of
    #: those rows points at a file this process cannot see.
    unreadable_music_roots: list[str] = dataclasses.field(default_factory=list)
    #: Discriminator for the stored record. The two import paths produce
    #: completely different numbers and a reader must not have to guess which
    #: it is holding from which keys happen to be present.
    kind: str = "rekordbox"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def run_import(
    data_dir: Path,
    *,
    emit: Emit,
    source: Path | None = None,
    limit: int | None = None,
    refresh_decrypt: bool = False,
) -> ImportOutcome:
    """Import rekordbox into ``data_dir``, reporting every stage.

    ``source`` overrides detection (the fixture path tests take). ``limit``
    caps the ingest for smoke runs. ``refresh_decrypt`` is the wizard's half
    of the CLI's ``--refresh-decrypt``. All three are on the HTTP endpoint,
    so an agent drives the identical flow the wizard drives.
    """
    started_at = _now()
    was_running = detect.rekordbox_is_running()

    if refresh_decrypt and source is None:
        # Re-decrypting means going back to the ENCRYPTED snapshot. Left to
        # detection, an existing plain copy would win the source race and the
        # refresh would quietly do nothing.
        snapshot = data_dir / detect.WORKING_COPY_NAME
        source = snapshot if snapshot.is_file() else None

    chosen, encrypted = _stage_detect(data_dir, emit, source)
    working = _stage_snapshot(data_dir, emit, chosen)
    plain = _stage_decrypt(data_dir, emit, working, refresh=refresh_decrypt)
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
        unreadable_music_roots=detect.denied_roots(detect.music_root_access()),
    )
    record.set_last_import(data_dir, outcome.to_dict())
    return outcome


@dataclasses.dataclass
class FolderImportOutcome:
    """What a folder import did. Every denominator is named separately.

    ``tracks_without_analysis`` is not a warning that got attached late: a
    folder import reads tags and nothing else, so this equals the number of
    tracks it wrote, and reporting it is what stops the result being mistaken
    for an analysed library.

    That count is only HALF the story, and reporting only that half read as a
    dead end to the first tester who saw it (test Mac run 2, Wed 16 Sep 2026).
    A folder import is the one path with no rekordbox ANLZ to read, so it is
    the path where own analysis is the only source there is: every track it
    writes lands rekordbox-unmapped and the daemon's analyze-on-import queue
    picks them up. ``analysis_available`` is therefore MEASURED on this engine
    rather than hardcoded False -- whether own analysis can actually run here
    is the fact that decides whether those tracks get a beatgrid at all, and
    an agent driving setup over HTTP reads it from the same place the wizard
    does.
    """

    started_at: str
    finished_at: str
    roots: list[str]
    unreadable_roots: list[str]
    files_seen: int
    files_dataless: int
    files_without_tags: int
    files_rejected_unplayable: int
    tracks: int
    tracks_written: int
    tracks_without_analysis: int
    analysis_available: bool = False
    analysis_detail: str = (
        "a folder import reads tags only: no BPM, no key and no beatgrid are "
        "written by the import itself, and none are guessed. Open DJ's own "
        "analysis supplies them afterwards; GET /api/v1/analysis-queue is the "
        "queue and POST /api/v1/analysis-queue/run starts the drain"
    )
    kind: str = "folder"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def run_folder_import(
    data_dir: Path,
    *,
    emit: Emit,
    roots: list[Path],
    limit: int | None = None,
) -> FolderImportOutcome:
    """Import audio files from ``roots``. No rekordbox anywhere in this path.

    The three failure modes it distinguishes: a folder that is not there, a
    folder macOS refuses to list, and a folder that is genuinely empty. The
    middle one is the whole reason this is not just a walk -- a blocked
    listing and an empty one look identical from ``os.walk``.
    """
    started_at = _now()
    if not roots:
        raise SetupImportError(
            detect.CODE_REKORDBOX_NOT_FOUND,
            "a folder import needs at least one folder to walk",
        )

    # Each line is emitted before the step it names. A stall keeps the last
    # line, so "loading ..." is that import, "probing" is the filesystem
    # probe, and "checking" means the probe has already returned.
    detect_at = FOLDER_STAGE_PROGRESS["detect"]
    emit(detect_at, "detect: preparing folder import")
    emit(detect_at, "detect: loading filesystem access")
    from apps.shared import fs_access
    emit(detect_at, "detect: loading state database")
    from apps.shared.state import db as state_db
    emit(detect_at, "detect: loading folder ingest")
    from apps.shared.state.ingest import folder as folder_ingest
    emit(detect_at, "detect: loading state writer")
    from apps.shared.state.writer import StateWriter
    emit(detect_at, f"detect: probing {len(roots)} folder(s)")
    probes = fs_access.probe_all(roots)
    emit(detect_at, f"detect: checking {len(probes)} folder(s)")
    denied = fs_access.denied_roots(probes)
    if denied and not any(probe.readable for probe in probes):
        raise SetupImportError(
            detect.CODE_ACCESS_DENIED,
            f"macOS refused to list {', '.join(denied)}. "
            f"{fs_access.GRANT_INSTRUCTIONS}",
        )
    missing = [probe.path for probe in probes if not probe.exists]
    if missing and not any(probe.readable for probe in probes):
        raise SetupImportError(
            detect.CODE_REKORDBOX_NOT_FOUND,
            f"no such folder: {', '.join(missing)}",
        )

    state_path = data_dir / "state" / detect.STATE_DB_NAME
    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, actor="setup-folder-import")
    try:
        report = folder_ingest.ingest_folder(
            writer,
            roots,
            dry_run=False,
            limit=limit,
            on_progress=_folder_progress(emit),
        )
    except MassMissingError as exc:
        raise SetupImportError(detect.CODE_INGEST_FAILED, str(exc)) from exc
    except PathCollisionError as exc:
        raise SetupImportError(detect.CODE_INGEST_FAILED, str(exc)) from exc
    finally:
        writer.close()
        conn.close()

    counts = detect.library_counts(data_dir)
    outcome = FolderImportOutcome(
        started_at=started_at,
        finished_at=_now(),
        roots=report.roots,
        unreadable_roots=report.unreadable_roots,
        files_seen=report.files_seen,
        files_dataless=report.files_dataless,
        files_without_tags=report.files_without_tags,
        files_rejected_unplayable=report.files_rejected_unplayable,
        tracks=counts.tracks,
        tracks_written=report.tracks_inserted + report.tracks_unchanged,
        tracks_without_analysis=report.tracks_without_analysis,
        analysis_available=_own_analysis_available(),
    )
    emit(
        FOLDER_STAGE_PROGRESS["ingest"],
        (
            f"ingest: {outcome.tracks_written} of {outcome.files_seen} "
            f"readable files imported, none analysed"
            + (
                f"; {outcome.files_rejected_unplayable} file(s) skipped as "
                f"unplayable"
                if outcome.files_rejected_unplayable
                else ""
            )
            + (
                f"; {len(outcome.unreadable_roots)} folder(s) could not be read"
                if outcome.unreadable_roots
                else ""
            )
        ),
    )
    record.set_last_import(data_dir, outcome.to_dict())
    return outcome


def _own_analysis_available() -> bool:
    """Can THIS engine run its own analysis, measured rather than assumed.

    Imported here rather than at module scope: importing the backend registry
    is cheap, but this module is loaded by the setup job worker in a
    subprocess, and a hard import would make a missing analysis extra a boot
    failure of the IMPORT instead of a reported capability of it.
    """
    from apps.analysis import backends

    return backends.default_backend_installed()


def _folder_progress(emit: Emit):
    """Map the ingest's ``(done, total, message)`` onto the job's 0..1 bar."""
    low = FOLDER_STAGE_PROGRESS["scan"]
    high = FOLDER_STAGE_PROGRESS["ingest"]

    def report(done: int, total: int, message: str) -> None:
        fraction = done / total if total > 0 else 1.0
        emit(low + (high - low) * fraction, f"ingest: {message}")

    return report


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


def _stage_decrypt(
    data_dir: Path, emit: Emit, working: Path, *, refresh: bool
) -> Path:
    """Hand back a plaintext database, reporting which of the two ways it got one.

    Delegates to ``apps.shared.rekordbox_db.ensure_plain_db``, which owns
    the SQLCipher routine and the reuse policy, and echoes its two outcomes
    in the same words ``ingest-rb`` prints. A decrypt is never silent here
    either: the operator sees whether a copy was rebuilt or reused.

    The detect stage's encryption verdict is deliberately NOT reused: the
    snapshot may have replaced the very file that verdict described, so the
    header is re-read against what is on disk now.
    """
    from apps.shared.rekordbox_db import RekordboxDecryptError, ensure_plain_db

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

    plain = data_dir / detect.PLAIN_COPY_NAME
    try:
        result, decrypted = ensure_plain_db(
            encrypted=working, plain=plain, refresh=refresh
        )
    except FileNotFoundError as exc:
        raise SetupImportError(
            detect.CODE_REKORDBOX_NOT_FOUND, str(exc)
        ) from exc
    except RekordboxDecryptError as exc:
        raise SetupImportError(
            detect.CODE_DECRYPT_FAILED,
            f"decrypting {working} to {plain} failed: {exc}",
        ) from exc

    # ensure_plain_db writes through a sibling temp file and renames, so a
    # file at the destination is a COMPLETE one. Re-reading the header still
    # earns its keep: a reused copy was written by some earlier run this
    # process cannot vouch for.
    if not detect.is_plain_sqlite(result):
        raise SetupImportError(
            detect.CODE_DECRYPT_FAILED,
            f"the decrypt step reported success but {result} is still not a "
            "plaintext SQLite file",
        )
    emit(
        STAGE_PROGRESS["decrypt"],
        (
            f"decrypt: decrypted {working.name} -> {result.name}"
            if decrypted
            else f"decrypt: reusing decrypted copy at {result}"
        ),
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


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = [
    "FOLDER_STAGES",
    "FOLDER_STAGE_PROGRESS",
    "STAGES",
    "STAGE_PROGRESS",
    "Emit",
    "FolderImportOutcome",
    "ImportOutcome",
    "SetupImportError",
    "run_folder_import",
    "run_import",
]
