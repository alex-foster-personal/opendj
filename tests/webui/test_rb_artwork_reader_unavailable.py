"""GET /artwork and rb-meta read embedded art WITHOUT mutagen (issue #4717).

The packaged app does not depend on mutagen (GPL). This proves a fresh
process serves an embedded picture through tinytag and that rb-meta agrees.

The embedded picture is written here by hand as an ID3v2.3 ``APIC`` frame
(spec section 4.15) around a real Pillow-encoded JPEG, so the fixture itself
needs no tag library either.

[if] embedded artwork is read with mutagen unimportable [then] the picture is served and a missing file is not available, [else stop].

Regression one-liners:
  - if this test needs mutagen installed to run then it can never prove the mutagen-less path
  - if a track with a real embedded cover 503s or 404s without mutagen then broken
  - if a missing file reports artwork_available True then broken (residency first)
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
NO_ART_SID = "c" * 40
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


def _jpeg() -> bytes:
    from io import BytesIO

    from PIL import Image

    out = BytesIO()
    Image.new("RGB", (32, 32), (200, 40, 90)).save(out, format="JPEG", quality=90)
    return out.getvalue()


def _syncsafe(n: int) -> bytes:
    return bytes(((n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F))


def _with_id3_apic(audio: bytes, image: bytes) -> bytes:
    """``audio`` with an ID3v2.3 tag holding one front-cover APIC frame."""
    body = b"\x00" + b"image/jpeg\x00" + b"\x03" + b"cover\x00" + image
    frame = b"APIC" + len(body).to_bytes(4, "big") + b"\x00\x00" + body
    return b"ID3\x03\x00\x00" + _syncsafe(len(frame)) + frame + audio


def test_artwork_served_when_mutagen_genuinely_cannot_be_imported(tmp_path: Path) -> None:
    image = _jpeg()
    with_art = tmp_path / "with art.mp3"
    with_art.write_bytes(_with_id3_apic((FIXTURE_ROOT / "src-320.mp3").read_bytes(), image))
    no_art = tmp_path / "no art.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", no_art)
    state_path = tmp_path / "state" / "state.db"
    _insert_local_track(state_path, STABLE_ID, str(with_art))
    _insert_local_track(state_path, NO_ART_SID, str(no_art))
    _insert_local_track(state_path, NO_FILE_SID, str(tmp_path / "does-not-exist.mp3"))
    image_path = tmp_path / "expected.jpg"
    image_path.write_bytes(image)

    probe = textwrap.dedent(f"""
        import os
        import sys
        os.environ["ODJ_ARTWORK_ONLINE"] = "0"

        from pathlib import Path

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from apps.adapters.rekordbox import config as rb_config
        from apps.webui.server.routes.rb_assets import router
        from apps.webui.server.sqlite_backend import make_backend

        rb_config.STATE_DB = Path({str(state_path)!r})
        rb_config.MASTER_PLAIN_DB = Path({str(tmp_path / "absent.db")!r})

        app = FastAPI()
        app.state.backend = make_backend(rb_config.STATE_DB)
        app.include_router(router, prefix="/api/v1")
        with TestClient(app) as client:
            art = client.get("/api/v1/tracks/{STABLE_ID}/artwork")
            bare = client.get("/api/v1/tracks/{NO_ART_SID}/artwork")
            stale = client.get("/api/v1/tracks/{NO_FILE_SID}/artwork")
            metas = [client.get(f"/api/v1/tracks/{{sid}}/rb-meta").json()["artwork_available"]
                     for sid in ({STABLE_ID!r}, {NO_ART_SID!r}, {NO_FILE_SID!r})]

        assert art.status_code == 200, art.text
        assert art.headers["content-type"] == "image/jpeg"
        assert art.content == Path({str(image_path)!r}).read_bytes()
        assert bare.status_code == 404, bare.text
        assert bare.json()["detail"]["code"] == "ARTWORK_NOT_FOUND"
        assert stale.status_code == 404, stale.text
        assert stale.json()["detail"]["code"] == "ARTWORK_NOT_FOUND"
        assert metas == [True, False, False], metas
        assert "mutagen" not in {{k for k, v in sys.modules.items() if v is not None}}
        print("PROBE_OK")
    """)
    result = subprocess.run(
        [sys.executable, "-"],
        input=probe,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "PROBE_OK" in result.stdout, f"stdout={result.stdout}\nstderr={result.stderr}"
