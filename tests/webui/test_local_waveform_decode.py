"""Locally decoded waveform peaks for a track with NO rekordbox mapping.

Cloud-buildable in the same shape as ``test_rb_meta_local_track.py``: a synthetic
state.db in tmp_path holding a track with a real on-disk WAV and no
``track_vendor_ids`` row -- the first-run state, needing no data/master.plain.db.

The audio is generated here: two seconds of a loud 60 Hz sine then two seconds of
digital silence, so a payload whose second half is not flat did not come from this
file. 60 Hz sits an octave and a half below the 200 Hz low/mid crossover
(NATIVE-06), so this fixture also says which BAND a value should be in: the low
band carries it and the other two are measured silence.

CI has no ffmpeg on PATH, so tests needing a real decode are marked ``requires_ffmpeg``
and skip there. The peak arithmetic, strip contract and cache revalidation are ALSO
covered directly off raw PCM and peak arrays, so the real-vs-invented check runs
everywhere, not only where a binary happens to exist.

Regression one-liners:
  - if /anlz for an unmapped track serves empty bands while ffmpeg can decode it then broken
  - if the served peaks don't follow the real loud-then-silent audio then broken
  - if the decode isn't cached under data/state/local-waveform-cache then broken
  - if a cached entry survives its audio file changing then broken
  - if a missing ffmpeg produces a waveform instead of an explicit not_decoded then broken
  - if undecodable audio produces a waveform instead of an explicit not_decoded then broken
  - if a browser row invents preview_b64 before the decode has run then broken
  - if a browser row lacks preview_b64 after the decode has run then broken
  - if an unknown stable_id stops 404ing TRACK_NOT_FOUND then broken
  - if an unset MDT_LIBRARY_MODE off darwin is swallowed into an empty lane then broken
  - if the three served bands are one envelope copied three times then broken
  - if a cache entry outlives a change to the file it was decoded from then broken
  - if the admission cap parks a request thread instead of an immediate not_decoded then broken
  - if a crash between the strip and entry writes leaves the entry but not the strip then broken
  - if a same-mtime, same-size rename to a new inode still reads as cache-current then broken
  - if an in-place rewrite with a restored mtime and same inode reads as cache-current then broken

[if] an unmapped track is decoded locally [then] peaks cache on disk and failures stay
explicit, [else stop].
"""
from __future__ import annotations

import base64
import math
import os
import struct
import sys
import wave
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.analysis_waveform import local_waveform
from apps.shared.state import db as state_db
from apps.webui.server import rb_vendor
from apps.webui.server.backend import Track
from apps.webui.server.routes.rb_assets import router
from apps.webui.server.sqlite_backend import make_backend
from tests import fs_clock

pytestmark = [pytest.mark.requirement("PARITY-03"), pytest.mark.rb_parity]

LOCAL_SID = "e" * 40
BROKEN_SID = "f" * 40
GONE_SID = "9" * 40
UNKNOWN_SID = "0" * 40
SAMPLE_RATE_HZ = 44_100
# Deep in the low band, far from the 200 Hz crossover.
TONE_HZ = 60.0
LOUD_S = 2.0
SILENT_S = 2.0
DURATION_MS = int((LOUD_S + SILENT_S) * 1000)


def _write_wav(path: Path, *, loud_s: float, silent_s: float) -> None:
    """A real WAV: ``loud_s`` of a full-scale 60 Hz sine, then digital silence."""
    frames = bytearray()
    for i in range(int(SAMPLE_RATE_HZ * loud_s)):
        value = int(32000 * math.sin(2 * math.pi * TONE_HZ * i / SAMPLE_RATE_HZ))
        frames += struct.pack("<h", value)
    frames += b"\x00\x00" * int(SAMPLE_RATE_HZ * silent_s)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE_HZ)
        handle.writeframes(bytes(frames))


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


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "imported track.wav"
    _write_wav(path, loud_s=LOUD_S, silent_s=SILENT_S)
    return path


@pytest.fixture
def broken_audio_file(tmp_path: Path) -> Path:
    """Real bytes ffmpeg genuinely cannot decode - not a mocked failure."""
    path = tmp_path / "not really audio.wav"
    path.write_bytes(b"this is not a RIFF header" * 4096)
    return path


@pytest.fixture
def client(
    tmp_path: Path,
    audio_file: Path,
    broken_audio_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    state_path = tmp_path / "state.db"
    _insert_local_track(state_path, LOCAL_SID, str(audio_file))
    _insert_local_track(state_path, BROKEN_SID, str(broken_audio_file))
    _insert_local_track(state_path, GONE_SID, str(tmp_path / "moved away.wav"))
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent.db")
    monkeypatch.setattr(
        rb_config, "LOCAL_WAVEFORM_CACHE_DIR", tmp_path / "local-waveform-cache"
    )
    monkeypatch.setenv("MDT_LIBRARY_MODE", "local")

    app = FastAPI()
    app.state.backend = make_backend(state_path)
    app.state.state_db_path = str(state_path)
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as test_client:
        yield test_client


def _anlz(client: TestClient, stable_id: str, points: int = 38400) -> dict:
    response = client.get(f"/api/v1/tracks/{stable_id}/anlz?points={points}")
    assert response.status_code == 200, response.text
    return response.json()


def _row(stable_id: str, file_path: str) -> dict:
    return rb_vendor.build_track_rows(
        [Track(stable_id=stable_id, duration_ms=DURATION_MS, file_path=file_path)]
    )[0]


# ----- the decode itself ------------------------------------------------------


@pytest.mark.requires_ffmpeg
def test_unmapped_track_serves_locally_decoded_peaks(client: TestClient) -> None:
    payload = _anlz(client, LOCAL_SID)
    local = payload["local_waveform"]
    assert local["status"] == "decoded"
    assert local["reason"] is None
    # The decoded strip rides back on this same response (issue #735) so a
    # browser row that already rendered can adopt it without a reload.
    assert len(base64.b64decode(local["preview_b64"])) == 360
    assert local["preview_max"] > 0
    assert payload["waveform"]["kind"] == "tri"
    detail = payload["waveform"]["detail"]
    assert detail["length"] > 0
    assert len(detail["low"]) == detail["length"]
    # A 60 Hz sine is low-band content. Three MEASURED bands say so; one
    # envelope copied three times could not (NATIVE-06).
    assert max(detail["low"]) > 0.8, "the low band must carry a 60 Hz sine"
    assert max(detail["mid"]) < 0.1 and max(detail["high"]) < 0.1, (
        "mid and high must be measured silence for a 60 Hz-only file, not a "
        f"copy of the low band; got mid={max(detail['mid'])}, "
        f"high={max(detail['high'])}"
    )
    assert payload["waveform"]["preview"]["length"] > 0


@pytest.mark.requires_ffmpeg
def test_peaks_follow_the_real_audio(client: TestClient) -> None:
    detail = _anlz(client, LOCAL_SID)["waveform"]["detail"]
    values = detail["low"]
    half = len(values) // 2
    loud = values[: half - 2]
    silent = values[half + 2 :]
    assert max(loud) > 0.8, "a full-scale sine must decode near 1.0"
    assert max(silent) == 0.0, "digital silence must decode flat, not synthesised"


@pytest.mark.requires_ffmpeg
def test_decode_is_cached_on_disk_and_reused_without_ffmpeg(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _anlz(client, LOCAL_SID)
    cache_dir = tmp_path / "local-waveform-cache"
    assert (cache_dir / f"{LOCAL_SID}.json").is_file()
    assert (cache_dir / f"{LOCAL_SID}.strip.json").is_file()
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries-here"))
    second = _anlz(client, LOCAL_SID)
    assert second["waveform"] == first["waveform"], "warm cache must not re-decode"
    assert second["local_waveform"]["status"] == "decoded"


@pytest.mark.requires_ffmpeg
def test_cache_entry_dies_with_its_audio_file(
    client: TestClient, audio_file: Path
) -> None:
    before = _anlz(client, LOCAL_SID)["waveform"]["detail"]["low"]
    assert max(before) > 0.8
    _write_wav(audio_file, loud_s=0.0, silent_s=3.0)
    after = _anlz(client, LOCAL_SID)["waveform"]["detail"]["low"]
    assert max(after) == 0.0, "the re-decode must follow the new bytes"
    assert before != after


# ----- honest failure states --------------------------------------------------


def test_missing_ffmpeg_yields_an_explicit_not_decoded(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pinned to ffmpeg: under the default ``auto`` a built engine decodes it
    # (the next test), which is the point of having one.
    monkeypatch.setenv("MDT_WAVEFORM_DECODER", "ffmpeg")
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries-here"))
    payload = _anlz(client, LOCAL_SID)
    assert payload["local_waveform"]["status"] == "not_decoded"
    assert "ffmpeg" in payload["local_waveform"]["reason"]
    assert payload["waveform"]["detail"] == {
        "length": 0, "low": [], "mid": [], "high": []
    }
    assert payload["waveform"]["preview"]["length"] == 0


def test_missing_ffmpeg_still_decodes_through_the_engine(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Own waveforms with nothing installed: ``odj-audio waveform`` alone."""
    from apps.analysis_waveform import decode

    try:
        decode.resolve_engine()
    except decode.LocalDecodeUnavailable as exc:
        pytest.skip(f"no odj-audio build here: {exc.reason}")
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries-here"))
    # Control first, while nothing is cached: forcing the missing ffmpeg is an
    # honest not_decoded, so the decoded state below comes from the engine.
    monkeypatch.setenv("MDT_WAVEFORM_DECODER", "ffmpeg")
    assert _anlz(client, LOCAL_SID)["local_waveform"]["status"] == "not_decoded"
    monkeypatch.delenv("MDT_WAVEFORM_DECODER")
    payload = _anlz(client, LOCAL_SID)
    assert payload["local_waveform"]["status"] == "decoded", payload["local_waveform"]
    assert payload["waveform"]["kind"] == "tri"
    assert max(payload["waveform"]["detail"]["low"]) > 0.8


@pytest.mark.requires_ffmpeg
def test_undecodable_audio_yields_an_explicit_not_decoded(client: TestClient) -> None:
    payload = _anlz(client, BROKEN_SID)
    assert payload["local_waveform"]["status"] == "not_decoded"
    assert payload["waveform"]["detail"]["length"] == 0


def test_missing_audio_file_yields_an_explicit_not_decoded(client: TestClient) -> None:
    payload = _anlz(client, GONE_SID)
    assert payload["local_waveform"]["status"] == "not_decoded"
    assert payload["waveform"]["detail"]["length"] == 0


@pytest.mark.skipif(
    sys.platform == "darwin", reason="unset MDT_LIBRARY_MODE is local on darwin"
)
def test_unset_library_mode_fails_loudly_rather_than_emptying_the_lane(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audio resolver carries the library-mode contract; a silently
    empty waveform lane would hide a host misconfiguration rather than report it."""
    monkeypatch.delenv("MDT_LIBRARY_MODE", raising=False)
    with pytest.raises(RuntimeError, match="MDT_LIBRARY_MODE"):
        client.get(f"/api/v1/tracks/{LOCAL_SID}/anlz")


def test_unknown_stable_id_still_404s_loudly(client: TestClient) -> None:
    response = client.get(f"/api/v1/tracks/{UNKNOWN_SID}/anlz")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "TRACK_NOT_FOUND"


# ----- the browser preview strip ----------------------------------------------


def test_browser_row_has_no_strip_before_the_decode_runs(
    client: TestClient, audio_file: Path
) -> None:
    row = _row(LOCAL_SID, str(audio_file))
    assert row["preview_b64"] is None
    assert row["preview_max"] is None


@pytest.mark.requires_ffmpeg
def test_browser_row_gains_the_decoded_strip(
    client: TestClient, audio_file: Path
) -> None:
    _anlz(client, LOCAL_SID)
    row = _row(LOCAL_SID, str(audio_file))
    raw = base64.b64decode(row["preview_b64"])
    assert len(raw) == 360, "120 cols x 3 bands, same contract as the ANLZ strip"
    assert row["preview_max"] == max(raw)
    assert max(raw[: 180]) > max(raw[180:]), "loud half then silent half"


# ----- the cache, with no ffmpeg in sight -------------------------------------
# The peak arithmetic, the band split and the cache VERSION key live in
# tests/analysis_waveform/ with the package that owns them (NATIVE-06); what
# stays here is the source-file revalidation the /anlz route depends on.


def _peaks(columns: int) -> np.ndarray:
    """``(columns, 3)`` tri-band peaks, each band a different constant."""
    return np.stack(
        [
            np.arange(columns, dtype=np.uint8),
            np.full(columns, 9, dtype=np.uint8),
            np.full(columns, 240, dtype=np.uint8),
        ],
        axis=1,
    )


def test_cache_entry_is_revalidated_against_its_source_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rb_config, "LOCAL_WAVEFORM_CACHE_DIR", tmp_path / "cache")
    source = tmp_path / "source.wav"
    _write_wav(source, loud_s=1.0, silent_s=0.0)
    peaks = _peaks(600)

    key = local_waveform._decode_key(source)
    local_waveform._store_peaks(LOCAL_SID, key, peaks)
    assert np.array_equal(local_waveform._cached_peaks(LOCAL_SID, key), peaks)
    assert local_waveform.local_preview_strip(LOCAL_SID)[0] is not None

    _write_wav(source, loud_s=2.0, silent_s=0.0)
    moved = local_waveform._decode_key(source)
    assert local_waveform._cached_peaks(LOCAL_SID, moved) is None
    assert local_waveform.local_preview_strip(LOCAL_SID) == (None, None), (
        "a strip must not outlive the bytes it was decoded from"
    )


def _seed_cached_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, os.stat_result]:
    """A source.wav with one cached peaks entry, ready for a replacement scenario."""
    monkeypatch.setattr(rb_config, "LOCAL_WAVEFORM_CACHE_DIR", tmp_path / "cache")
    source = tmp_path / "source.wav"
    _write_wav(source, loud_s=1.0, silent_s=0.0)
    key = local_waveform._decode_key(source)
    local_waveform._store_peaks(LOCAL_SID, key, _peaks(600))
    assert local_waveform._cached_peaks(LOCAL_SID, key) is not None
    return source, source.stat()


def test_a_same_mtime_same_size_replacement_is_still_detected_via_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rename-based replace (routes/relocate.py's own pattern) can preserve mtime
    and size - inode still moves (discussion_r3907973976; same fix as
    apps/vocals/cache.py, Sat 8 Aug 2026)."""
    source, original_stat = _seed_cached_source(tmp_path, monkeypatch)
    replacement = tmp_path / "source.wav.tmp"
    _write_wav(replacement, loud_s=1.0, silent_s=0.0)
    os.utime(replacement, (original_stat.st_atime, original_stat.st_mtime))
    os.replace(replacement, source)
    new_stat = source.stat()
    assert new_stat.st_mtime == original_stat.st_mtime
    assert new_stat.st_size == original_stat.st_size
    assert new_stat.st_ino != original_stat.st_ino, "must land on a new inode to test the fix"
    assert local_waveform._cached_peaks(LOCAL_SID, local_waveform._decode_key(source)) is None


@pytest.mark.skipif(os.name != "posix", reason="ctime is POSIX-only; see _source_key")
def test_an_in_place_overwrite_with_restored_mtime_is_still_detected_via_ctime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same inode, mtime restored via os.utime - ctime still moves (discussion_r3908286640)."""
    source, original_stat = _seed_cached_source(tmp_path, monkeypatch)
    with open(source, "r+b") as fh:
        fh.write(b"\x00")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    # The ctime this test is ABOUT has to have actually moved. Inode times come
    # from the kernel's coarse clock, so on a fast machine the seed, the
    # overwrite and the restore can all land inside one timer tick and leave
    # ctime numerically unchanged - the cache then hits for a reason that has
    # nothing to do with the code under test. Re-asserting the SAME restored
    # mtime keeps the precondition below exact while ctime advances.
    new_stat = fs_clock.stamp_until(
        source,
        lambda stat: stat.st_ctime_ns != original_stat.st_ctime_ns,
        "ctime moved while the restored mtime stayed put",
        times_ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    assert new_stat.st_mtime_ns == original_stat.st_mtime_ns
    assert new_stat.st_size == original_stat.st_size
    assert new_stat.st_ino == original_stat.st_ino, "same inode: testing the in-place gap"
    assert local_waveform._cached_peaks(LOCAL_SID, local_waveform._decode_key(source)) is None


def test_a_crash_mid_publish_leaves_the_cache_recoverable_not_half_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_store_peaks writes two files; if the process dies between them, the
    ENTRY file (what _cached_peaks gates on) must be the one still missing, or a
    crash leaves the entry present and the strip sidecar permanently absent."""
    monkeypatch.setattr(rb_config, "LOCAL_WAVEFORM_CACHE_DIR", tmp_path / "cache")
    source = tmp_path / "source.wav"
    _write_wav(source, loud_s=1.0, silent_s=0.0)
    key = local_waveform._decode_key(source)
    peaks = _peaks(600)

    real_write_json = local_waveform._write_json
    calls = 0

    def _write_json_dies_on_second_call(path: Path, entry: dict) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated crash mid-publish")
        real_write_json(path, entry)

    monkeypatch.setattr(local_waveform, "_write_json", _write_json_dies_on_second_call)
    with pytest.raises(OSError, match="simulated crash mid-publish"):
        local_waveform._store_peaks(LOCAL_SID, key, peaks)

    assert local_waveform._strip_path(LOCAL_SID).exists(), (
        "the strip must be written first so a crash cannot lose it"
    )
    assert not local_waveform._entry_path(LOCAL_SID).exists(), (
        "the entry must be the one still missing after a crash"
    )
    assert local_waveform._cached_peaks(LOCAL_SID, key) is None, (
        "a missing entry must read as a cache MISS so the next request "
        "recomputes and republishes both files, instead of the half-"
        "published pair reading as done forever"
    )


# ----- decode admission (thread-pool protection) -------------------------------


def test_admission_cap_answers_not_decoded_instead_of_parking_a_thread(
    tmp_path: Path, audio_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saturating the real admission semaphore (not a mock) must turn away the
    next caller immediately, not park it behind ffmpeg or the queue-wait timeout."""
    state_path = tmp_path / "state.db"
    _insert_local_track(state_path, LOCAL_SID, str(audio_file))
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent.db")
    monkeypatch.setattr(rb_config, "LOCAL_WAVEFORM_CACHE_DIR", tmp_path / "cache")
    holders = [
        local_waveform._DECODE_ADMISSION.acquire(blocking=False)
        for _ in range(local_waveform.MAX_DECODE_WAITERS)
    ]
    assert all(holders), "test setup must actually saturate the real admission gate"
    try:
        with pytest.raises(local_waveform.LocalDecodeUnavailable, match="already in flight"):
            local_waveform.ensure_local_peaks(LOCAL_SID)
    finally:
        for _ in holders:
            local_waveform._DECODE_ADMISSION.release()


def test_queue_wait_timeout_answers_not_decoded_instead_of_hanging(
    tmp_path: Path, audio_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With both real decode slots genuinely held, a request must give up after
    DECODE_QUEUE_WAIT_S, not hang for the full 180s ffmpeg timeout."""
    state_path = tmp_path / "state.db"
    _insert_local_track(state_path, LOCAL_SID, str(audio_file))
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent.db")
    monkeypatch.setattr(rb_config, "LOCAL_WAVEFORM_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(local_waveform, "DECODE_QUEUE_WAIT_S", 0.2)
    held = [
        local_waveform._DECODE_SLOTS.acquire(blocking=False)
        for _ in range(local_waveform.MAX_CONCURRENT_DECODES)
    ]
    assert all(held), "test setup must actually hold both real decode slots"
    try:
        with pytest.raises(local_waveform.LocalDecodeUnavailable, match="no decode slot"):
            local_waveform.ensure_local_peaks(LOCAL_SID)
    finally:
        for _ in held:
            local_waveform._DECODE_SLOTS.release()


# ----- retryable vs permanent not_decoded ---------------------------------------


def test_saturated_admission_answers_retryable_and_is_never_cached(
    client: TestClient,
) -> None:
    """A transient decoder-saturation reject is per-request, not per-track: the
    payload says retryable: true and the route sends Cache-Control: no-store."""
    holders = [
        local_waveform._DECODE_ADMISSION.acquire(blocking=False)
        for _ in range(local_waveform.MAX_DECODE_WAITERS)
    ]
    assert all(holders), "test setup must actually saturate the real admission gate"
    try:
        response = client.get(f"/api/v1/tracks/{LOCAL_SID}/anlz")
    finally:
        for _ in holders:
            local_waveform._DECODE_ADMISSION.release()
    assert response.status_code == 200, response.text
    local = response.json()["local_waveform"]
    assert local["status"] == "not_decoded"
    assert local["retryable"] is True
    assert response.headers["cache-control"] == "no-store"


def test_missing_ffmpeg_not_decoded_is_not_retryable_and_stays_revalidatable(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A track this host genuinely cannot decode is a fact about the track, not
    a queue hiccup: no retryable claim, and the response is still held (not
    dropped outright like the saturated-admission case above), just revalidated
    rather than replayed blind. `public, max-age=3600` predates c7004cdc0/
    1e5fdf2d0, which made every non-retryable /anlz response `private, no-cache`
    plus an ETag: this endpoint's beatgrid can change under a local track too
    (the own-beatgrid overlay applies on this same branch), so nothing about the
    LOCAL_SID fixture makes it exempt from the promotion-safety fix those
    commits describe (Codex P1 BLOCKING, PR #1587)."""
    monkeypatch.setenv("MDT_WAVEFORM_DECODER", "ffmpeg")  # no engine fallback
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries-here"))
    response = client.get(f"/api/v1/tracks/{LOCAL_SID}/anlz")
    assert response.status_code == 200, response.text
    local = response.json()["local_waveform"]
    assert local["status"] == "not_decoded"
    assert local.get("retryable") is not True
    # Storable-and-revalidated, not `no-store`: that is what separates this
    # permanent fact from the transient retryable case above. PARITY-02 made the
    # body depend on a process-local toggle that does not appear in the URL, so
    # the freshness rule is `no-cache` plus an ETag rather than a 1h max-age
    # (discussion_r3970967302); the distinction this test exists for is intact.
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["etag"]


@pytest.mark.requires_ffmpeg
def test_successful_decode_stays_revalidatable(client: TestClient) -> None:
    response = client.get(f"/api/v1/tracks/{LOCAL_SID}/anlz")
    assert response.status_code == 200, response.text
    assert response.json()["local_waveform"]["status"] == "decoded"
    assert response.headers["cache-control"] == "private, no-cache"
    etag = response.headers["etag"]
    assert etag

    # The bytes still stay off the wire when nothing changed, which is the half
    # of "cacheable" this test was written to protect.
    revalidated = client.get(
        f"/api/v1/tracks/{LOCAL_SID}/anlz", headers={"If-None-Match": etag}
    )
    assert revalidated.status_code == 304
    assert revalidated.content == b""
