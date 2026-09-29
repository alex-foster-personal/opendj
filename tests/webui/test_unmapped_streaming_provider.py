"""Listing rows name their streaming provider even with no rekordbox mapping.

CHROME-02 (Codex P2 on PR #3896): a djay-only or locally imported streaming
row has no rekordbox mapping, so the browser never loads its rb-meta and never
sees a FolderPath. The row itself must carry the provider, or the table falls
back to the generic cloud icon.

[if] an unmapped tidal:/soundcloud: row is listed [then] its row carries that
provider, [else stop].
[if] a local file row is listed [then] it carries no provider, [else stop].

How the library gets here, and why (Codex 4129637635):

- No monkeypatching and no in-process config override. A disposable data dir
  is hydrated, then a CHILD interpreter boots the production engine
  composition root from it (``build_config`` -> ``apply_env_contract`` ->
  ``prepare_layout`` -> ``create_app``, the path the justfile's
  ``engine_openapi_dump`` uses) with ``MDT_DATA_DIR`` set before any import,
  so rekordbox config resolves the dir the way the daemon does. Every listing
  the browser renders rows from is read through that app.
- No rekordbox database at all: the rows under test are the ones that HAVE no
  rekordbox mapping, so there is nothing to map them to.
- The present-audio rows point at the committed real mp3 that
  tests/test_rb_assets.py also uses, checksum-verified before the copy, not
  fabricated bytes.
- The state rows are written through the production ``StateWriter`` API, the
  same calls an import writes. There is no locked real snapshot holding
  unmapped Tidal or SoundCloud rows: the rekordbox snapshot
  (tests/fixtures/rekordbox.manifest.json) holds only mapped rows, and the only
  streaming fixture in tests/fixtures (conformance/09-streaming-only) calls its
  URI synthetic. When one exists, it replaces _hydrate_state.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared import platform_paths
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter

pytestmark = [pytest.mark.requirement("CHROME-02")]

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_AUDIO = REPO_ROOT / "tests" / "fixtures" / "phase7-dedup" / "src-320.mp3"
REAL_AUDIO_SHA256 = "3614b47af8c2d7ae2696f97b7f3e9658fbf26f68ed7f6854c5909b77d62c44b0"

UNMAPPED_TIDAL = "a" * 40
UNMAPPED_SOUNDCLOUD = "b" * 40
UNMAPPED_HTTP = "c" * 40
UNMAPPED_LOCAL = "d" * 40
# A stale streaming URI whose audio IS present locally: not streaming, so no provider.
UNMAPPED_URI_PRESENT = "e" * 40
PLAYLIST_ID = "pl-unmapped-streaming"
ALL_IDS = {UNMAPPED_TIDAL, UNMAPPED_SOUNDCLOUD, UNMAPPED_HTTP, UNMAPPED_LOCAL, UNMAPPED_URI_PRESENT}

# Every listing wire shape the browser renders rows from. The playlist detail
# and search build TrackRowOut(**row); /tracks (All Tracks, and its q= search)
# copies fields one by one into TrackListItemOut, which is where Codex found
# the provider dropped (comment 4129520596).
LISTINGS = {
    "playlist": (f"/api/v1/playlists/{PLAYLIST_ID}", "tracks"),
    "tracks": ("/api/v1/tracks?limit=50", "items"),
    "tracks-q": ("/api/v1/tracks?q=track&limit=50", "items"),
    "search": ("/api/v1/search?q=track&limit=50", "items"),
}

# Runs in the child, after MDT_DATA_DIR is in its environment.
_ENGINE_READ = """
import json, sys
from tests.testclient_host_allowlist import install_loopback_testclient_default
install_loopback_testclient_default()
from apps.engine_core.config import apply_env_contract, build_config, prepare_layout
cfg = build_config(sys.argv[1], "127.0.0.1", 8683)
apply_env_contract(cfg)
prepare_layout(cfg)
from fastapi.testclient import TestClient
from apps.engine_core.app import create_app
listings = json.loads(sys.argv[2])
out = {}
with TestClient(create_app(cfg)) as client:
    for name, (url, key) in listings.items():
        response = client.get(url)
        out[name] = {"status": response.status_code, "rows": response.json().get(key)}
print(json.dumps(out))
"""


def _hydrate_state(data_dir: Path) -> None:
    audio = data_dir / "audio" / "local.mp3"
    audio.parent.mkdir(parents=True)
    digest = hashlib.sha256(REAL_AUDIO.read_bytes()).hexdigest()
    assert digest == REAL_AUDIO_SHA256, f"{REAL_AUDIO} changed: {digest}"
    shutil.copy2(REAL_AUDIO, audio)
    conn = state_db.open_rw(data_dir / "state" / "state.db")
    try:
        writer = StateWriter(conn, actor="unit-test")
        specs = [
            (UNMAPPED_TIDAL, "tidal:track:123"),
            (UNMAPPED_SOUNDCLOUD, "soundcloud:tracks:456"),
            (UNMAPPED_HTTP, "https://example.com/stream.mp3"),
            (UNMAPPED_LOCAL, str(audio)),
            (UNMAPPED_URI_PRESENT, "spotify:track:now-local"),
        ]
        for sid, file_path in specs:
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=f"track {sid[0]}",
                artists=["Artist"],
                album=None,
                isrc=None,
                duration_ms=180_000,
                file_path=file_path,
            )
        writer.upsert_track_location(
            stable_id=UNMAPPED_URI_PRESENT, kind="local", file_path=str(audio)
        )
        writer.insert_playlist(
            playlist_id=PLAYLIST_ID,
            name="Unmapped streaming",
            vendor="djay",
            vendor_pl_id="djay-pl-1",
        )
        writer.set_playlist_memberships(PLAYLIST_ID, [sid for sid, _ in specs])
        writer.close()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def engine_listings(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    data_dir = tmp_path_factory.mktemp("engine-data")
    _hydrate_state(data_dir)
    env = {
        **os.environ,
        "MDT_DATA_DIR": str(data_dir),
        "MDT_LIBRARY_MODE": "local",
        "PYTHONPATH": str(REPO_ROOT),
    }
    proc = subprocess.run(
        [sys.executable, "-c", _ENGINE_READ, str(data_dir), json.dumps(LISTINGS)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(params=sorted(LISTINGS))
def listing(request: pytest.FixtureRequest) -> str:
    return request.param


def _rows(engine_listings: dict[str, dict], listing: str) -> dict[str, dict]:
    result = engine_listings[listing]
    assert result["status"] == 200, result
    rows = {row["stable_id"]: row for row in result["rows"]}
    # Presence, not absence: every hydrated row must come back, or a test
    # below could pass on a row the endpoint never returned.
    assert set(rows) >= ALL_IDS, f"{listing} returned {sorted(rows)}"
    return rows


@pytest.mark.parametrize(
    ("stable_id", "provider"),
    [(UNMAPPED_TIDAL, "tidal"), (UNMAPPED_SOUNDCLOUD, "soundcloud"), (UNMAPPED_HTTP, "unknown")],
)
def test_unmapped_streaming_row_carries_its_provider(
    engine_listings: dict[str, dict], listing: str, stable_id: str, provider: str
) -> None:
    """if an unmapped streaming row loses its provider on any listing then broken"""
    row = _rows(engine_listings, listing)[stable_id]
    assert row["has_rb_mapping"] is False, "the case under test is an UNMAPPED row"
    assert row["is_streaming"] is True
    assert row["streaming_provider"] == provider


def test_local_row_carries_no_provider(engine_listings: dict[str, dict], listing: str) -> None:
    """control: a present local file is not streaming and names no provider"""
    row = _rows(engine_listings, listing)[UNMAPPED_LOCAL]
    assert row["has_rb_mapping"] is False
    assert row["file_exists"] is True
    assert row["is_streaming"] is False
    assert row["streaming_provider"] is None


def test_present_audio_behind_a_streaming_uri_names_no_provider(
    engine_listings: dict[str, dict], listing: str
) -> None:
    """control: the provider follows is_streaming, not the bare URI prefix"""
    row = _rows(engine_listings, listing)[UNMAPPED_URI_PRESENT]
    assert row["has_rb_mapping"] is False
    assert row["file_exists"] is True
    assert row["is_streaming"] is False
    assert row["streaming_provider"] is None


@pytest.mark.parametrize(
    ("path", "provider"),
    [
        ("spotify:track:x", "spotify"),
        ("tidal:track:x", "tidal"),
        ("soundcloud:tracks:x", "soundcloud"),
        ("http://example.com/a", "unknown"),
        ("https://example.com/a", "unknown"),
        ("/music/a.mp3", None),
        ("", None),
        (None, None),
    ],
)
def test_streaming_provider_parses_the_shared_prefix_set(
    path: str | None, provider: str | None
) -> None:
    """if a non-streaming path names a provider, or a streaming URI names none, then broken"""
    assert platform_paths.streaming_provider(path) == provider
