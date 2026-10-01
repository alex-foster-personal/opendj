"""GET /artwork for a locally imported track (no rekordbox vendor mapping).

Cloud-buildable: synthetic state.db in tmp_path, no data/master.plain.db
needed -- same pattern as ``tests/webui/test_rb_meta_local_track.py``. Before
this (PARITY-TODO.md line 137), ``/artwork`` 404'd VENDOR_MAPPING_NOT_FOUND
for every unmapped row even when the file itself carries a real embedded
cover. It now falls back to reading that embedded tag directly, mirroring
the existing ``/anlz`` and ``/rb-meta`` VENDOR_MAPPING_NOT_FOUND fallbacks.

A real MP3 fixture gets a REAL APIC frame written by the in-house ID3v2
writer and read back by tinytag -- never synthesised bytes -- so the test
proves actual tag parsing, not a stub.

Regression one-liners:
  - if /artwork 404s for an unmapped track with real embedded art then broken
  - if /artwork serves anything but the real embedded jpeg bytes then broken
  - if /artwork omits a byte-derived ETag or ignores If-None-Match then broken
  - if /artwork still 404s ARTWORK_NOT_FOUND for an unmapped track with NO embedded art then broken
  - if rb-meta.artwork_available stays False once embedded art exists then broken
  - if an embedded picture exceeds the response memory limit then /artwork
    404s and rb-meta reports it unavailable then broken
  - if a genuinely unknown stable_id stops 404ing TRACK_NOT_FOUND then broken

The tag reader (tinytag) is a core dependency, so there is no "reader
unavailable" verdict any more; ``tests/shared/test_no_gpl_tag_library.py``
proves artwork is read with mutagen made unimportable.
"""
from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared import id3v2
from apps.shared.state import db as state_db
from apps.webui.server.routes.rb_assets import router
from apps.webui.server.sqlite_backend import make_backend
from tests.fixtures import tagged_audio as ta
from tests.fixtures.conftest import resolve_required_fixture

pytestmark = [
    pytest.mark.requirement("CAT-05"),
    pytest.mark.rb_parity,
]

WITH_ART_SID = "e" * 40
NO_ART_SID = "f" * 40
UNKNOWN_SID = "0" * 40
DURATION_MS = 240_000

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"


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


@pytest.fixture(scope="module")
def jpeg_bytes() -> bytes:
    """Real JPEG from the rekordbox USB export fixtures, not synthesised.

    Routes through resolve_required_fixture() (rather than a hard-coded repo
    path) so this CAT-05 acceptance test keeps finding the fixture, and keeps
    refusing to silently skip, once the in-repo directory leaves and only
    ``rb-usb-export.extern`` remains (PR #718).
    """
    root = resolve_required_fixture("rb-usb-export")
    artwork_jpeg = root / "PIONEER" / "Artwork" / "00009" / "a169.jpg"
    data = artwork_jpeg.read_bytes()
    assert data[:2] == b"\xff\xd8"
    return data


@pytest.fixture
def track_with_art(tmp_path: Path, jpeg_bytes: bytes) -> Path:
    dst = tmp_path / "has art.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", dst)
    ta.add_apic(dst, jpeg_bytes, mime="image/jpeg")
    return dst


@pytest.fixture
def track_without_art(tmp_path: Path) -> Path:
    dst = tmp_path / "no art.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", dst)
    return dst


@pytest.fixture
def client(
    tmp_path: Path,
    track_with_art: Path,
    track_without_art: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    state_path = tmp_path / "state.db"
    _insert_local_track(state_path, WITH_ART_SID, str(track_with_art))
    _insert_local_track(state_path, NO_ART_SID, str(track_without_art))
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent.db")

    app = FastAPI()
    app.state.backend = make_backend(state_path)
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as test_client:
        yield test_client


def test_serves_real_embedded_jpeg_bytes(
    client: TestClient, jpeg_bytes: bytes
) -> None:
    resp = client.get(f"/api/v1/tracks/{WITH_ART_SID}/artwork")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == jpeg_bytes


def test_embedded_artwork_is_revalidatable(
    client: TestClient, jpeg_bytes: bytes
) -> None:
    first = client.get(f"/api/v1/tracks/{WITH_ART_SID}/artwork")
    assert first.status_code == 200, first.text
    etag = first.headers["etag"]
    assert etag == f'"{hashlib.sha256(jpeg_bytes).hexdigest()}"'

    revalidated = client.get(
        f"/api/v1/tracks/{WITH_ART_SID}/artwork",
        headers={"If-None-Match": etag},
    )

    assert revalidated.status_code == 304
    assert revalidated.headers["etag"] == etag
    assert revalidated.headers["cache-control"] == "public, max-age=86400"
    assert revalidated.content == b""

    wildcard = client.get(
        f"/api/v1/tracks/{WITH_ART_SID}/artwork",
        headers={"If-None-Match": "*"},
    )
    assert wildcard.status_code == 304

    changed = client.get(
        f"/api/v1/tracks/{WITH_ART_SID}/artwork",
        headers={"If-None-Match": '"different-artwork"'},
    )
    assert changed.status_code == 200
    assert changed.content == jpeg_bytes


@pytest.mark.parametrize("size", ["s", "m", "orig"])
def test_size_param_still_accepted_for_embedded_path(
    client: TestClient, jpeg_bytes: bytes, size: str
) -> None:
    resp = client.get(
        f"/api/v1/tracks/{WITH_ART_SID}/artwork", params={"size": size}
    )
    assert resp.status_code == 200
    assert resp.content == jpeg_bytes


def test_404s_artwork_not_found_when_no_embedded_art(client: TestClient) -> None:
    resp = client.get(f"/api/v1/tracks/{NO_ART_SID}/artwork")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "ARTWORK_NOT_FOUND"


def test_unknown_stable_id_still_404s_track_not_found(client: TestClient) -> None:
    resp = client.get(f"/api/v1/tracks/{UNKNOWN_SID}/artwork")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "TRACK_NOT_FOUND"


def test_rb_meta_artwork_available_true_once_embedded_art_exists(
    client: TestClient,
) -> None:
    resp = client.get(f"/api/v1/tracks/{WITH_ART_SID}/rb-meta")
    assert resp.status_code == 200, resp.text
    assert resp.json()["artwork_available"] is True


def test_rb_meta_artwork_available_false_without_embedded_art(
    client: TestClient,
) -> None:
    resp = client.get(f"/api/v1/tracks/{NO_ART_SID}/rb-meta")
    assert resp.status_code == 200, resp.text
    assert resp.json()["artwork_available"] is False


def test_oversized_embedded_artwork_is_not_served_or_advertised(
    client: TestClient, track_with_art: Path, jpeg_bytes: bytes
) -> None:
    """The route and rb-meta share the embedded-artwork size ceiling."""
    tag = id3v2.load_or_new(track_with_art)
    tag.remove(lambda frame: frame.frame_id == "APIC")
    id3v2.save(track_with_art, tag)
    ta.add_apic(
        track_with_art, jpeg_bytes + b"\x00" * (4 * 1024 * 1024), mime="image/jpeg", desc="oversized cover"
    )

    artwork = client.get(f"/api/v1/tracks/{WITH_ART_SID}/artwork")
    assert artwork.status_code == 404
    assert artwork.json()["detail"]["code"] == "ARTWORK_NOT_FOUND"

    meta = client.get(f"/api/v1/tracks/{WITH_ART_SID}/rb-meta")
    assert meta.status_code == 200, meta.text
    assert meta.json()["artwork_available"] is False
