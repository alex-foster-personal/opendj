"""GET /artwork's ARTWORK_READER_UNAVAILABLE 503, proved WITHOUT mutagen.

Deliberately split out of ``test_rb_artwork_local_track.py``: that module
carries ``pytestmark = pytest.mark.requires_mutagen`` because most of its
tests write real embedded tags via mutagen's own writer. This module has no
such marker and its fixture never touches mutagen at all -- a plain file
copy is enough, since :func:`apps.adapters.rekordbox.paths.local_artwork`
raises the 503 BEFORE any tag read is attempted.

Codex caught this live on PR #773 (P1/BLOCKING): the shipped desktop payload
omits the optional ``tags`` extra (GPL vs this wheel's Apache license), so
the one acceptance test that proves the reader-unavailable fallback works
must itself be provable in an environment that genuinely lacks the reader --
gating it behind ``requires_mutagen`` meant it could only ever run in an
environment that HAS mutagen, silently proving nothing about the one
environment it exists to cover (AGENTS.md: "Never silently skip acceptance
because ... a platform is missing").

Regression one-liner:
  - if this test needs mutagen installed to run then it can never prove the mutagen-less path
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from apps.shared.state import db as state_db

pytestmark = [pytest.mark.requirement("CAT-05"), pytest.mark.rb_parity]

STABLE_ID = "e" * 40
NO_FILE_SID = "d" * 40
DURATION_MS = 240_000
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _insert_local_track(path: Path, stable_id: str, file_path: str) -> None:
    conn = state_db.open_rw(path)
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, "
            "file_path, created_at, updated_at) "
            "VALUES (?, 'inferred', ?, ?, '2026-01-01', '2026-01-01')",
            (stable_id, DURATION_MS, file_path),
        )
        conn.commit()
    finally:
        conn.close()


def test_artwork_verdicts_when_mutagen_genuinely_cannot_be_imported(
    tmp_path: Path,
) -> None:
    """Both missing-reader verdicts, under one genuinely absent reader.

    Two fixture-based tests used to live here, each monkeypatching the cached
    ``HAS_MUTAGEN`` boolean. Codex raised both as P1/BLOCKING on PR #773 and was
    right twice: flipping the flag MANUFACTURES the precondition, so on a machine
    where mutagen is installed neither test could show what happens when the
    reader is really gone. That is the fail-closed prohibition in AGENTS.md
    L55-L59, and it hides exactly the packaged-build case (no optional ``tags``
    extra) these tests name.

    So both verdicts are asserted here instead, in a FRESH subprocess that blocks
    the real ``import mutagen`` with ``sys.modules["mutagen"] = None`` BEFORE
    ``apps.shared._mutagen`` has ever run, so the production ``except
    ImportError`` branch fires rather than a flag:

    - a file that EXISTS with no reader -> 503 ``ARTWORK_READER_UNAVAILABLE``,
      an UNKNOWN rather than a guessed absence;
    - a file that DOES NOT exist -> 404 ``ARTWORK_NOT_FOUND``, because residency
      is decided before reader availability.

    The second is the ordering assertion, and it is only worth something because
    the reader is genuinely gone rather than flagged off. ``assert HAS_MUTAGEN is
    False`` inside the subprocess is the control: if the import block ever stops
    working, this fails loudly instead of quietly testing nothing.

    Same subprocess also proves GET /rb-meta's ``artwork_available`` under the
    same genuinely blocked import (issue #795): the resolvable-but-unreadable
    row must report ``None`` ("could not check"), not the guessed ``False``
    that :func:`apps.adapters.rekordbox.paths.local_artwork_available` used to
    collapse it into -- the exact reason the UI never called ``/artwork`` and
    the 503 above went unheard. The missing-file row is the overshoot control:
    residency still wins, so it stays a real ``False``, not ``None``.
    """
    audio_path = tmp_path / "no reader.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", audio_path)
    state_path = tmp_path / "state.db"
    _insert_local_track(state_path, STABLE_ID, str(audio_path))
    # Second row, same db: a path that resolves to nothing on disk. Both
    # verdicts are then read under ONE genuinely blocked import.
    _insert_local_track(state_path, NO_FILE_SID, str(tmp_path / "does-not-exist.mp3"))
    absent_master_db = tmp_path / "absent.db"

    script = tmp_path / "probe_mutagen_absent.py"
    script.write_text(
        textwrap.dedent(f"""
            import sys
            sys.modules["mutagen"] = None  # force a genuine ImportError, not a flag flip

            from pathlib import Path

            from fastapi import FastAPI
            from fastapi.testclient import TestClient

            from apps.adapters.rekordbox import config as rb_config
            from apps.shared._mutagen import HAS_MUTAGEN

            assert HAS_MUTAGEN is False, "mutagen import was not actually blocked"

            from apps.webui.server.routes.rb_assets import router
            from apps.webui.server.sqlite_backend import make_backend

            rb_config.STATE_DB = Path({str(state_path)!r})
            rb_config.MASTER_PLAIN_DB = Path({str(absent_master_db)!r})

            app = FastAPI()
            app.state.backend = make_backend()
            app.include_router(router, prefix="/api/v1")
            with TestClient(app) as test_client:
                resp = test_client.get("/api/v1/tracks/{STABLE_ID}/artwork")
                stale = test_client.get("/api/v1/tracks/{NO_FILE_SID}/artwork")
                meta = test_client.get("/api/v1/tracks/{STABLE_ID}/rb-meta")
                stale_meta = test_client.get("/api/v1/tracks/{NO_FILE_SID}/rb-meta")

            # A file that exists with no reader: capability UNKNOWN, not a guess.
            assert resp.status_code == 503, resp.text
            assert resp.json()["detail"]["code"] == "ARTWORK_READER_UNAVAILABLE"

            # A file that does not exist: residency is decided FIRST, so absence
            # wins over the missing reader. This is the ordering assertion, and it
            # is only worth anything because the reader is genuinely gone here
            # rather than flagged off.
            assert stale.status_code == 404, stale.text
            assert stale.json()["detail"]["code"] == "ARTWORK_NOT_FOUND"

            # rb-meta must agree with /artwork about the epistemic state (#795):
            # a resolvable file with no reader is UNKNOWN, never a guessed False.
            assert meta.status_code == 200, meta.text
            assert meta.json()["artwork_available"] is None
            # Overshoot control: a genuinely missing file stays a real False,
            # not None -- residency still wins even with the reader gone.
            assert stale_meta.status_code == 200, stale_meta.text
            assert stale_meta.json()["artwork_available"] is False
            print("PROBE_OK")
        """),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "PROBE_OK" in result.stdout, f"stdout={result.stdout}\nstderr={result.stderr}"
