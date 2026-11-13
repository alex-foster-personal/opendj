from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter

from apps.webui.frontend.tests.e2e.support.deckload_fixture_audio import ensure_audio
from apps.webui.frontend.tests.e2e.support.deckload_fixture_model import (
    AUDIO_SUBDIR,
    EMPTY_PLAYLIST_ID,
    FEEDBACK_SUBDIR,
    FIXTURE_REVISION,
    POPULATED_PLAYLIST_ID,
    REPOSITORY_ROOT,
    REVISION_MARKER,
)

# ----- real ingest -----------------------------------------------------------
def _ingest_and_verify(
    data_dir: Path, audio_dir: Path, files: list[Path], label: str
) -> list[tuple[str, str | None, str | None]]:
    """Run the REAL ingest until the DB holds one row per audio file.

    Shared by ``build`` and ``build_autoplay_chain`` so the "N rows for N
    files" invariant and the readable-``file_path`` check have exactly one
    implementation. ``label`` only names the caller in the failure message.
    """
    state_db_path = data_dir / "state" / "state.db"
    rows = _track_rows(state_db_path) if state_db_path.is_file() else []
    if len(rows) != len(files):
        _state_cli(data_dir, "init")
        _state_cli(data_dir, "ingest-folder", "--root", str(audio_dir), "--write")
        rows = _track_rows(state_db_path)
    if len(rows) != len(files):
        raise SystemExit(
            f"[ERROR] {label} ingest wrote {len(rows)} track(s) for "
            f"{len(files)} audio file(s) in {audio_dir}"
        )
    for stable_id, _title, file_path in rows:
        if not file_path or not Path(file_path).is_file():
            raise SystemExit(
                f"[ERROR] track {stable_id} has no readable file_path: {file_path!r}"
            )
    return rows


def _uv() -> str:
    """uv by resolved path (portable-by-default: never a bare program name)."""
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("[ERROR] uv not found on PATH; the fixture builder runs the real CLIs through it")
    return uv


def _state_cli(data_dir: Path, *args: str) -> None:
    """Run the REAL shared-state CLI against this fixture data dir."""
    env = dict(os.environ)
    env["MDT_DATA_DIR"] = str(data_dir)
    # The engine refuses WEB_CONCURRENCY; keep the builder's env identical to
    # the one the engine will boot under so a surprise cannot hide here.
    env.pop("WEB_CONCURRENCY", None)
    command = [
        _uv(),
        "run",
        "--no-sync",
        "python",
        "-m",
        "apps.shared.state.cli",
        *args,
    ]
    result = subprocess.run(
        command, cwd=REPOSITORY_ROOT, env=env, capture_output=True, text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"[ERROR] {' '.join(args)} failed with exit code {result.returncode}"
        )
    sys.stdout.write(result.stdout)


def _track_rows(state_db: Path) -> list[tuple[str, str | None, str | None]]:
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        return [
            (str(sid), title, file_path)
            for sid, title, file_path in conn.execute(
                "SELECT stable_id, title, file_path FROM tracks ORDER BY title"
            )
        ]
    finally:
        conn.close()


def _discard_stale_revision(data_dir: Path) -> None:
    """Wipe a fixture dir built by an older revision.

    Reusing it would measure audio that no longer matches what the spec
    asserts, and the failure would look like a product bug rather than a
    stale fixture, so this is a wipe and not a warning.
    """
    marker = data_dir / REVISION_MARKER
    current = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    if current == str(FIXTURE_REVISION):
        return
    for stale in (data_dir / AUDIO_SUBDIR, data_dir / "state"):
        if stale.exists():
            shutil.rmtree(stale)
    data_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{FIXTURE_REVISION}\n", encoding="utf-8")


def _reset_feedback_dir(data_dir: Path) -> None:
    """Wipe any feedback state left by a prior or interrupted run.

    Unlike ``AUDIO_SUBDIR``/"state", this is not cached content gated on
    ``FIXTURE_REVISION``: it is mutation state the SERVER writes when a spec
    dispatches a real feedback_mark/pin/archive command, so it is reset on
    EVERY call regardless of revision. A run killed after
    `performance-feedback-card-dismiss.spec.ts` seeds a pin but before its
    `finally` archives it would otherwise leave that pin in place forever,
    and the next run's `button.fb-pin[title^=...]` locator would then match
    both the stale pin and the new one, breaking Playwright's strict-mode
    single-element assumption (Codex P2 BLOCKING, #1628).
    """
    feedback_dir = data_dir / FEEDBACK_SUBDIR
    if feedback_dir.exists():
        shutil.rmtree(feedback_dir)


def _seed_playlists(
    state_db_path: Path, rows: list[tuple[str, str | None, str | None]]
) -> None:
    """Write browser-addressable populated and empty playlists through StateWriter."""
    stable_ids = [stable_id for stable_id, _title, _file_path in rows]
    if len(stable_ids) < 2:
        raise SystemExit("[ERROR] playlist fixture needs at least two ingested tracks")
    conn = state_db.open_rw(state_db_path)
    writer = StateWriter(conn, actor="e2e-deckload-fixture")
    try:
        writer.insert_playlist(
            playlist_id=POPULATED_PLAYLIST_ID,
            name="E2E Fixture Set",
            vendor="fixture",
            vendor_pl_id=POPULATED_PLAYLIST_ID,
        )
        writer.set_playlist_memberships(POPULATED_PLAYLIST_ID, stable_ids)
        writer.insert_playlist(
            playlist_id=EMPTY_PLAYLIST_ID,
            name="E2E Empty Set",
            vendor="fixture",
            vendor_pl_id=EMPTY_PLAYLIST_ID,
        )
        writer.set_playlist_memberships(EMPTY_PLAYLIST_ID, [])
    finally:
        writer.close()
        conn.close()

