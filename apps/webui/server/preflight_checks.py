"""PREFLIGHT-01 (issue #771): the checks behind ``GET /api/v1/preflight``.

Pulled out of the route module so the checks are testable without a FastAPI
app and importable by the CLI/ship-flow probe this issue's own notes ask
for (#766's OPS-05 audio-byte probe is meant to consume this same logic
rather than growing a second one).

Checks, cheap-first, matching the issue text exactly:

  1. engine-alive -- implicit (the endpoint answered at all); still a row so
     the UI has something to draw for it.
  2. state-db -- opens ``state.db`` fresh off disk and reads ``schema_meta``
     directly, independent of whatever the already-constructed backend
     decided at boot (catches #762, and a schema regression introduced by a
     backup restore WHILE the engine is already running).
  3. audio-access -- reads exactly one byte (range 0-0) from a KNOWN track
     location (``tracks.file_path`` off state.db, the same column the deck
     load contract test in ``test_audio_deck_load_contract.py`` seeds)
     under a hard wall-clock timeout in a killable subprocess (catches #766
     and #2749: a blocked ``open()`` must time out for the caller without
     leaking a wedged in-process thread).
  4. library-attached -- track count > 0, UNLESS setup was explicitly
     dismissed with an empty library (PREFLIGHT-02, issue #2589), in which
     case an empty library is the honest, expected state and the row is
     ``pending`` rather than ``fail`` -- see below.

PREFLIGHT-02 (issue #2589): a brand-new install where the operator clicked
"Continue without importing" has zero tracks ON PURPOSE. Before this,
``library-attached`` failed on 0 tracks unconditionally, with no notion of
that decision, so the boot gate re-locked itself the moment the setup
overlay closed -- a dismissed, empty library and a genuinely broken one
rendered the identical ``fail``. The dismissed flag lives engine-side in
``<data_dir>/setup.json`` (:mod:`apps.engine_core.setup.record`), so this
check now reads it: dismissed-and-empty is ``pending`` (non-blocking, same
vocabulary the audio-access check already uses for "nothing to report a
verdict on"), and NOT-dismissed-and-empty stays ``fail`` -- the setup
wizard, not this gate, is the surface that ask belongs on.

  [if] setup was dismissed and the library has 0 tracks [then] the row is
    ``pending`` with a remediation naming "Run setup", never ``fail``
    [else ⛔️]
  [if] setup was NOT dismissed and the library has 0 tracks [then] the row
    stays ``fail`` exactly as before (the wizard, not this gate, owns that
    state) [else ⛔️]
  [if] the setup record cannot be parsed [then] the row is ``fail`` with the
    real parse error, never a silent "not dismissed" guess [else ⛔️]

AUDIO-ACCESS HAS TWO DISTINCT NEGATIVE OUTCOMES, deliberately not collapsed
into one:

  * ``pending`` -- no sampled track carries ANY recorded file location at
    all, so there is nothing to even attempt (a metadata-only import, or
    every location genuinely unset). Honest denominator, never a fake
    ``pass``.
  * ``pending`` (again) -- every sampled location is MISSING on disk
    (``ENOENT``/``ENOTDIR``: a library imported from another machine or
    home directory, or files moved since). That is a library problem, not
    an access problem, and it must never hold the boot gate. Missing
    files are skipped and the next sampled location is tried. Private
    library census and first-run incident details are not included here.
  * ``fail`` -- a sampled location EXISTS and opening it fails, whether
    that is a permission ``OSError`` (``EACCES``/``EPERM``) or the hard
    timeout (#766's blocked-``open()`` shape). Both point at the macOS
    Media Library permission.

Every ``fail`` detail carries the real underlying exception string. No
result is ever synthesized: a check that could not run reports ``pending``
or ``fail`` with the real reason, never a silently-swallowed pass.
"""

from __future__ import annotations

import errno
import sqlite3
import time
from pathlib import Path

from apps.engine_core.setup import detect as setup_detect
from apps.engine_core.setup import record as setup_record
from apps.engine_core.setup.detect import detect_rekordbox
from apps.shared import platform_paths
from apps.shared.bounded_file_open import AUDIO_ACCESS_TIMEOUT_S, probe_readable_byte
from apps.shared.state import db as state_db
from apps.shared.state import locations as state_locations
from apps.shared.state import schema as state_schema

from .models import PreflightCheckOut, PreflightOut
from .sqlite_backend import read_tracks_schema_version

# Bounded sample so a huge library never makes this check itself the slow
# part of boot; cheap-first applies to how MUCH work each check does too.
AUDIO_SAMPLE_LIMIT: int = 20
# A missing file is a library that moved, never a macOS permission denial:
# skip it and try the next sampled location instead of holding the boot gate.
MISSING_FILE_ERRNOS: frozenset[int] = frozenset({errno.ENOENT, errno.ENOTDIR})
MISSING_LIBRARY_REMEDIATION = (
    "The sampled tracks point at files that no longer exist at their recorded "
    "location (a library imported from another machine or home directory). "
    "Relink or re-import the library; playback of those tracks will fail until then."
)

# Apple's Media & Apple Music privacy pane. macOS opens System Settings
# straight to this pane from the ``x-apple.systempreferences:`` scheme; a
# prior-deny grant never re-prompts on its own, so the "Re-request
# permissions" control always offers this link alongside retrying the real
# read (#766's remediation ask).
MEDIA_LIBRARY_PRIVACY_PANE_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_MediaLibrary"
)
AUDIO_ACCESS_REMEDIATION = (
    "macOS may be blocking Media Library access. Clicking Re-request "
    "permissions attempts the read again, which prompts macOS if it has "
    "never asked before; if nothing prompts (a prior denial), open System "
    f"Settings directly: {MEDIA_LIBRARY_PRIVACY_PANE_URL}"
)
LIBRARY_ATTACHED_REMEDIATION = "Import your music to get started."
LIBRARY_ATTACHED_REKORDBOX_UNREACHABLE_REMEDIATION = (
    "rekordbox is installed but its library database was not found at {path} "
    "(the drive may be unmounted). Import a folder instead, or reconnect the "
    "drive and re-check."
)
#: PREFLIGHT-02 (#2589): the dismissed-and-empty branch is deliberately a
#: different sentence, not a copy of the sentence above -- that one still
#: reads as a problem ("no state.db"/"0 tracks", "Run setup") for a state
#: that IS the wizard doing exactly what "Continue without importing" asked
#: for. This one says so.
LIBRARY_ATTACHED_DISMISSED_REMEDIATION = (
    "Setup was dismissed with an empty library. Click Run setup to import "
    "one, or keep using the app empty."
)
LIBRARY_ATTACHED_USER_REMEDIATION = (
    "Import your music from a folder or from rekordbox to get started."
)
LIBRARY_ATTACHED_DISMISSED_USER_REMEDIATION = (
    "You chose to continue without importing. Click Import your music whenever "
    "you are ready, or keep using the app empty."
)
LIBRARY_ATTACHED_REKORDBOX_UNREACHABLE_USER_REMEDIATION = (
    "We found rekordbox on this computer, but its library drive is not "
    "connected. Plug in the drive, or import a folder instead."
)


# ----- severity and explainers -------------------------------------------
#: Which rows can hold the boot gate, and which are just worth saying.
#: Severity distinguishes blocking failures from advisory outcomes.
#:
#: blocking: the app cannot usefully run until it passes.
#: advisory: the app runs, and the user is told. An empty library is the
#:   clearest case -- "continue without importing" is a supported choice, so
#:   a red light that holds the whole window is the app calling the user's own
#:   decision an error.
#: Severity is per OUTCOME, not per row. Three of these four rows fail only
#: when something is genuinely wrong (no engine, an unopenable database, a
#: permission macOS refused), and those keep holding the gate. The ONE
#: legitimate state that used to look like a failure is an empty library:
#: "continue without importing" is a supported choice, and a rekordbox drive
#: that is merely unplugged is not a defect in Open DJ. Those branches say
#: `severity="advisory"` at their own construction site.
#:
#: This table therefore only supplies the DEFAULT, and the default is
#: blocking: a new check that nobody classified holds the gate rather than
#: being quietly downgraded to a colour nobody acts on.
CHECK_SEVERITY: dict[str, str] = {
    "engine-alive": "blocking",
    "state-db": "blocking",
    "audio-access": "blocking",
    "library-attached": "blocking",
}

#: What each row means and what to do about it, in one sentence, shown on
#: hover for EVERY status. A user must be able to ask "what is this?" without
#: having to break it first, which is what remediation-only copy forces.
CHECK_EXPLAINER: dict[str, str] = {
    "engine-alive": (
        "Open DJ runs a small engine on your Mac that holds your library and "
        "plays audio. If this is red, quit Open DJ and open it again."
    ),
    "state-db": (
        "Your library lives in one file on this Mac. If this is red, restart "
        "Open DJ once; it repairs the file on the way up."
    ),
    "audio-access": (
        "macOS asks permission before an app reads your music files. If this "
        "is red, click Re-request permissions and allow the prompt. Orange "
        "means there was nothing to test yet, which is fine."
    ),
    "library-attached": (
        "How many tracks Open DJ can see. Orange just means none yet: import "
        "your music whenever you like, or keep using the app empty."
    ),
}


def _apply_severity(check: PreflightCheckOut) -> PreflightCheckOut:
    """Stamp the severity and explainer for a check, by id.

    Unknown ids stay blocking on purpose: a new check that nobody classified
    holds the gate rather than being quietly downgraded to a colour nobody
    acts on.
    """
    update: dict[str, object] = {"explainer": CHECK_EXPLAINER.get(check.id)}
    # A branch that already declared itself advisory keeps that; everything
    # else takes the row default. model_fields_set is what distinguishes a
    # deliberate "advisory" from the field's own default, so a future
    # default flip cannot silently erase a branch's declaration.
    if "severity" not in check.model_fields_set:
        update["severity"] = CHECK_SEVERITY.get(check.id, "blocking")
    return check.model_copy(update=update)


# ----- individual checks --------------------------------------------------
def _with_user_copy(
    check: PreflightCheckOut,
    *,
    user_label: str,
    user_detail: str,
    user_remediation: str | None = None,
) -> PreflightCheckOut:
    return check.model_copy(
        update={
            "user_label": user_label,
            "user_detail": user_detail,
            "user_remediation": user_remediation if user_remediation is not None else check.remediation,
        }
    )


def _rekordbox_library_unreachable(data_dir: Path) -> bool:
    """True when rekordbox appears installed but no database is reachable."""
    rb_dir = platform_paths.rekordbox_app_dir()
    if not rb_dir.is_dir():
        return False
    detection = setup_detect.detect_rekordbox(data_dir)
    if detection.installed:
        return False
    return setup_detect.CODE_REKORDBOX_NOT_FOUND in detection.blockers


def _engine_alive() -> PreflightCheckOut:
    return _with_user_copy(
        PreflightCheckOut(
            id="engine-alive",
            label="Engine alive",
            status="pass",
            detail="the endpoint answered",
        ),
        user_label="App connected",
        user_detail="The app is running.",
    )


def _state_db(state_db_path: Path) -> PreflightCheckOut:
    if not state_db_path.is_file():
        return _with_user_copy(
            PreflightCheckOut(
                id="state-db",
                label="State database",
                status="pass",
                detail=f"no state.db yet at {state_db_path} (fresh install, nothing to migrate)",
            ),
            user_label="Library database",
            user_detail="Ready for your first import.",
        )
    try:
        has_tracks, version = read_tracks_schema_version(
            state_db_path, busy_timeout_s=state_db.DEFAULT_BUSY_TIMEOUT_S,
        )
    except sqlite3.Error as exc:
        return _with_user_copy(
            PreflightCheckOut(
                id="state-db",
                label="State database",
                status="fail",
                detail=f"{state_db_path} could not be opened: {exc}",
                remediation="Restart the app; if this persists, restore state.db from a backup.",
            ),
            user_label="Library database",
            user_detail="Needs attention before your library can load.",
            user_remediation="Restart the app. If this keeps happening, restore your library from a backup.",
        )
    if not has_tracks:
        return _with_user_copy(
            PreflightCheckOut(
                id="state-db",
                label="State database",
                status="pass",
                detail=f"{state_db_path} has no tracks table yet (fresh install)",
            ),
            user_label="Library database",
            user_detail="Ready for your first import.",
        )
    if version < state_schema.SCHEMA_VERSION:
        return _with_user_copy(
            PreflightCheckOut(
                id="state-db",
                label="State database",
                status="fail",
                detail=(
                    f"schema_meta reports version {version}, expected {state_schema.SCHEMA_VERSION}"
                ),
                remediation=(
                    "Restart the app to let it auto-migrate, or run "
                    "`python -m apps.shared.state.cli init` on this state.db."
                ),
            ),
            user_label="Library database",
            user_detail="Needs an update before your library can load.",
            user_remediation="Restart the app to finish updating your library database.",
        )
    return _with_user_copy(
        PreflightCheckOut(
            id="state-db",
            label="State database",
            status="pass",
            detail=f"schema_meta version {version} (expected {state_schema.SCHEMA_VERSION})",
        ),
        user_label="Library database",
        user_detail="Ready.",
    )


def _track_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL").fetchone()[0]


def _sample_track_locations(conn: sqlite3.Connection, limit: int) -> list[str | None]:
    rows = conn.execute(
        "SELECT stable_id FROM tracks WHERE deleted_at IS NULL ORDER BY stable_id LIMIT ?",
        (limit,),
    ).fetchall()
    sample: list[str | None] = []
    for (stable_id,) in rows:
        sample.append(state_locations.recorded_audio_path(conn, str(stable_id)))
    return sample


def _audio_access(state_db_path: Path) -> PreflightCheckOut:
    if not state_db_path.is_file():
        return _with_user_copy(
            PreflightCheckOut(
                id="audio-access",
                label="Audio access",
                status="pending",
                detail="no state.db yet; nothing to sample",
            ),
            user_label="Music file access",
            user_detail="Nothing to check until you import music.",
        )
    try:
        conn = state_db.open_ro(state_db_path)
        try:
            total = _track_count(conn)
            sample = _sample_track_locations(conn, AUDIO_SAMPLE_LIMIT)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _with_user_copy(
            PreflightCheckOut(
                id="audio-access",
                label="Audio access",
                status="fail",
                detail=f"{state_db_path} could not be queried: {exc}",
                remediation="See the state-db check; the schema may be stale.",
            ),
            user_label="Music file access",
            user_detail="Could not read your library to check file access.",
            user_remediation="See the library database check above, then try again.",
        )

    if total == 0:
        return _with_user_copy(
            PreflightCheckOut(
                id="audio-access",
                label="Audio access",
                status="pending",
                detail="0 of 0 tracks in the library; nothing to sample",
            ),
            user_label="Music file access",
            user_detail="Nothing to check until you import music.",
        )

    recorded = [Path(p) for p in sample if p]
    if not recorded:
        return _with_user_copy(
            PreflightCheckOut(
                id="audio-access",
                label="Audio access",
                status="pending",
                detail=(
                    f"0 of {len(sample)} sampled tracks ({total} total) have a recorded file location"
                ),
            ),
            user_label="Music file access",
            user_detail="Your tracks do not have file locations recorded yet.",
        )
    missing: list[Path] = []
    start = time.monotonic()
    for picked_path in recorded:
        result = probe_readable_byte(picked_path, timeout_s=AUDIO_ACCESS_TIMEOUT_S)
        if result.outcome == "timeout":
            return _with_user_copy(
                PreflightCheckOut(
                    id="audio-access",
                    label="Audio access",
                    status="fail",
                    detail=(
                        f"opening {picked_path} did not return within {AUDIO_ACCESS_TIMEOUT_S:.0f}s"
                    ),
                    remediation=AUDIO_ACCESS_REMEDIATION,
                ),
                user_label="Music file access",
                user_detail="macOS may be blocking access to your music files.",
                user_remediation=(
                    "Click Re-request permissions, or open System Settings to allow "
                    "Media & Apple Music access for this app."
                ),
            )
        if result.outcome == "error":
            if result.errno in MISSING_FILE_ERRNOS:
                missing.append(picked_path)
                continue
            message = result.message or "open failed"
            return _with_user_copy(
                PreflightCheckOut(
                    id="audio-access",
                    label="Audio access",
                    status="fail",
                    detail=f"{picked_path}: {message}",
                    remediation=AUDIO_ACCESS_REMEDIATION,
                ),
                user_label="Music file access",
                user_detail="macOS may be blocking access to your music files.",
                user_remediation=(
                    "Click Re-request permissions, or open System Settings to allow "
                    "Media & Apple Music access for this app."
                ),
            )
        elapsed_ms = (time.monotonic() - start) * 1000
        skipped = f", {len(missing)} sampled location(s) missing on disk" if missing else ""
        return _with_user_copy(
            PreflightCheckOut(
                id="audio-access",
                label="Audio access",
                status="pass",
                detail=f"read 1 byte from a sampled track in {elapsed_ms:.0f}ms{skipped}",
            ),
            user_label="Music file access",
            user_detail="Your music files can be read.",
        )
    return _with_user_copy(
        PreflightCheckOut(
            id="audio-access",
            label="Audio access",
            status="pending",
            detail=(
                f"0 of {len(recorded)} sampled tracks ({total} total) exist on disk, "
                f"e.g. {missing[0]}; the library points at moved files, not a permission problem"
            ),
            remediation=MISSING_LIBRARY_REMEDIATION,
        ),
        user_label="Music file access",
        user_detail="Some tracks point at files that are no longer on this computer.",
        user_remediation="Relink or re-import those tracks to play them here.",
    )


def _data_dir_for_state_db(state_db_path: Path) -> Path:
    """The data dir ``setup.json`` lives in, derived from ``state.db``'s path.

    Same idiom ``app_wiring.py``'s lyric-index watcher already uses: state.db
    is always ``<data_dir>/state/state.db``, so its grandparent is the data
    dir unless the layout is nonstandard, in which case the parent is the
    least-wrong guess rather than a crash.
    """
    return state_db_path.parent.parent if state_db_path.parent.name == "state" else state_db_path.parent


def _rekordbox_unreachable_remediation(data_dir: Path) -> str | None:
    """rekordbox's Pioneer folder exists but master.db is missing -- usually an
    unmounted library drive, not a machine with no DJ software at all."""
    app_dir = platform_paths.rekordbox_app_dir()
    if not app_dir.is_dir():
        return None
    detection = detect_rekordbox(data_dir)
    if detection.live_db.exists:
        return None
    return LIBRARY_ATTACHED_REKORDBOX_UNREACHABLE_REMEDIATION.format(
        path=detection.live_db.path
    )


def _library_user_remediation(data_dir: Path, dismissed: bool) -> str:
    if dismissed:
        return LIBRARY_ATTACHED_DISMISSED_USER_REMEDIATION
    if _rekordbox_library_unreachable(data_dir):
        return LIBRARY_ATTACHED_REKORDBOX_UNREACHABLE_USER_REMEDIATION
    return LIBRARY_ATTACHED_USER_REMEDIATION


def _library_attached_remediation(data_dir: Path, dismissed: bool) -> str:
    if dismissed:
        return LIBRARY_ATTACHED_DISMISSED_REMEDIATION
    unreachable = _rekordbox_unreachable_remediation(data_dir)
    if unreachable is not None:
        return unreachable
    return LIBRARY_ATTACHED_REMEDIATION


def _library_attached(state_db_path: Path, data_dir: Path) -> PreflightCheckOut:
    try:
        dismissed = setup_record.read(data_dir).dismissed
    except setup_record.SetupRecordError as exc:
        # A setup record that cannot be parsed is a real defect (record.py's
        # own module doc: "defaulting it away would silently re-show a
        # wizard the operator already dismissed"). Surface it as a fail row
        # with the actual parse error rather than guessing either way.
        return _with_user_copy(
            PreflightCheckOut(
                id="library-attached",
                label="Library attached",
                status="fail",
                detail=f"the setup record at {setup_record.record_path(data_dir)} could not be read: {exc}",
                remediation="Restore or delete the setup record in the data dir, then restart.",
            ),
            user_label="Your music library",
            user_detail="Setup settings could not be read.",
            user_remediation="Restart the app. If this keeps happening, reset setup in the data folder.",
        )

    user_remediation = _library_user_remediation(data_dir, dismissed)

    if not state_db_path.is_file():
        if dismissed:
            return _with_user_copy(
                PreflightCheckOut(
                    id="library-attached",
                    label="Library attached",
                    status="pending",
                    detail=(
                        f"no state.db at {state_db_path} yet; setup was dismissed with an "
                        "empty library"
                    ),
                    remediation=LIBRARY_ATTACHED_DISMISSED_REMEDIATION,
                ),
                user_label="Your music library",
                user_detail="No music imported yet.",
                user_remediation=user_remediation,
            )
        user_detail = (
            "No music imported yet."
            if not _rekordbox_library_unreachable(data_dir)
            else "rekordbox is installed, but its library is not reachable right now."
        )
        return _with_user_copy(
            PreflightCheckOut(
                id="library-attached",
                label="Library attached",
                status="fail",
                detail=f"no state.db at {state_db_path}",
                remediation=_library_attached_remediation(data_dir, dismissed=False),
            ),
            user_label="Your music library",
            user_detail=user_detail,
            user_remediation=user_remediation,
        )
    try:
        conn = state_db.open_ro(state_db_path)
        try:
            total = _track_count(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _with_user_copy(
            PreflightCheckOut(
                id="library-attached",
                label="Library attached",
                status="fail",
                detail=f"{state_db_path} could not be queried: {exc}",
                remediation="See the state-db check; the schema may be stale.",
            ),
            user_label="Your music library",
            user_detail="Could not read your library.",
            user_remediation="See the library database check above, then try again.",
        )
    if total == 0:
        if dismissed:
            return _with_user_copy(
                PreflightCheckOut(
                    id="library-attached",
                    label="Library attached",
                    status="pending",
                    detail="0 tracks in the library; setup was dismissed",
                    remediation=LIBRARY_ATTACHED_DISMISSED_REMEDIATION,
                    # The user asked for exactly this. Holding the whole
                    # window on it calls their own choice an error.
                    severity="advisory",
                ),
                user_label="Your music library",
                user_detail="No tracks yet.",
                user_remediation=user_remediation,
            )
        user_detail = (
            "No tracks in your library yet."
            if not _rekordbox_library_unreachable(data_dir)
            else "rekordbox is installed, but its library is not reachable right now."
        )
        return _with_user_copy(
            PreflightCheckOut(
                id="library-attached",
                label="Library attached",
                status="fail",
                detail="0 tracks in the library",
                remediation=_library_attached_remediation(data_dir, dismissed=False),
                # An empty library on a fresh install is where every user
                # starts, and an unplugged rekordbox drive is not a defect in
                # Open DJ. Both are worth saying (orange, and the import call
                # to action stays on screen); neither is worth a red window
                # the user cannot get past.
                severity="advisory",
            ),
            user_label="Your music library",
            user_detail=user_detail,
            user_remediation=user_remediation,
        )
    return _with_user_copy(
        PreflightCheckOut(
            id="library-attached",
            label="Library attached",
            status="pass",
            detail=f"{total} tracks",
        ),
        user_label="Your music library",
        user_detail=f"{total} tracks ready.",
    )


def run_preflight(state_db_path: Path) -> PreflightOut:
    data_dir = _data_dir_for_state_db(state_db_path)
    checks = [
        _engine_alive(),
        _state_db(state_db_path),
        _audio_access(state_db_path),
        _library_attached(state_db_path, data_dir),
    ]
    checks = [_apply_severity(c) for c in checks]
    blocking_failed = any(
        c.status == "fail" and c.severity == "blocking" for c in checks
    )
    # An advisory row is worth SAYING, whether it failed or could not be
    # exercised. Counting only fails would hide the pending audio-access row,
    # which is the one a user most often needs to act on.
    advisories = sum(
        1 for c in checks if c.severity == "advisory" and c.status != "pass"
    )
    status = "fail" if blocking_failed else "pass"
    return PreflightOut(status=status, advisories=advisories, checks=checks)


__all__ = [
    "AUDIO_ACCESS_TIMEOUT_S",
    "AUDIO_SAMPLE_LIMIT",
    "CHECK_EXPLAINER",
    "CHECK_SEVERITY",
    "LIBRARY_ATTACHED_DISMISSED_REMEDIATION",
    "LIBRARY_ATTACHED_REKORDBOX_UNREACHABLE_REMEDIATION",
    "LIBRARY_ATTACHED_REMEDIATION",
    "MEDIA_LIBRARY_PRIVACY_PANE_URL",
    "run_preflight",
]
