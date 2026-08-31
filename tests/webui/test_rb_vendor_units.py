"""Pure-unit tests for rb_vendor parsing helpers (no local rekordbox data).

Covers the SPIKE-A1/B1 decode logic with synthetic inputs so CI exercises it
even without data/master.plain.db.

Regression one-liners:
  - if _vocal_regions doesn't merge runs separated by < 1.5 s then broken
  - if _vocal_regions keeps regions shorter than 1.0 s then broken
  - if region intensity isn't the max PVDI value in the merged run then broken
  - if read_pvdi accepts a mutated fixed header instead of raising then broken
  - if vocals_payload doesn't return exactly the three contract states then broken
  - if a warm vocals_for_content row re-reads the .2EX then broken
  - if a re-analyzed .2EX (new mtime) keeps serving the old regions then broken
  - if mutating a returned vocals payload poisons the next caller then broken
  - if _peak_downsample_cols isn't a per-bucket max (transient-preserving) then broken
  - if keep_by_availability doesn't map all/true/false explicitly then broken
  - if a crash mid anlz-cache write can leave truncated JSON behind then broken
  - if concurrent anlz-cache writers can corrupt staging or fail then broken
"""
from __future__ import annotations

import json
import os
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

from apps.adapters.rekordbox import config as rb_config
from apps.webui.server import rb_vendor
from apps.webui.server.rb_vendor_pkg import anlz as rb_anlz
from apps.webui.server.routes.tracks import keep_by_availability

pytestmark = pytest.mark.requirement("CAT-05")

FPS = 22050 / 1024  # 21.53 -- the only rate PVDI has ever been observed at


def _frames(seconds: float) -> int:
    return round(seconds * FPS)


# ----- _vocal_regions ----------------------------------------------------------

def test_vocal_regions_merges_sub_gap_runs() -> None:
    envelope = bytes(
        [2] * _frames(1.4) + [0] * _frames(0.5) + [4] * _frames(2.0)
    )
    regions = rb_vendor._vocal_regions(envelope, FPS)
    assert len(regions) == 1, "0.5 s gap < 1.5 s merge window must fuse runs"
    assert regions[0]["intensity"] == 4, "intensity = max across the merged run"
    assert regions[0]["start_s"] == 0.0
    assert regions[0]["end_s"] == pytest.approx(3.9, abs=0.15)


def test_vocal_regions_keeps_super_gap_runs_apart_and_drops_short() -> None:
    envelope = bytes(
        [2] * _frames(1.4) + [0] * _frames(2.0) + [3] * _frames(0.7)
    )
    regions = rb_vendor._vocal_regions(envelope, FPS)
    assert len(regions) == 1, (
        "2.0 s gap must split; the trailing 0.7 s run is < 1.0 s min region"
    )
    assert regions[0]["intensity"] == 2


def test_vocal_regions_threshold_is_intensity_min() -> None:
    below = bytes([rb_vendor.VOCAL_INTENSITY_MIN - 1] * _frames(5.0))
    assert rb_vendor._vocal_regions(below, FPS) == []
    at = bytes([rb_vendor.VOCAL_INTENSITY_MIN] * _frames(5.0))
    assert len(rb_vendor._vocal_regions(at, FPS)) == 1


# ----- read_pvdi / vocals_payload on synthetic .2EX files ------------------------

def _synthetic_2ex(envelope: bytes, fixed_header: bytes | None = None) -> bytes:
    """Minimal PMAI container with a single PVDI section (B1 section 3 layout)."""
    fixed = fixed_header if fixed_header is not None else rb_vendor._PVDI_FIXED_HEADER
    section = (
        b"PVDI"
        + struct.pack(">II", 24, 24 + len(envelope))
        + fixed
        + struct.pack(">I", len(envelope))
        + envelope
    )
    head_len = 28
    file_len = head_len + len(section)
    header = b"PMAI" + struct.pack(">II", head_len, file_len)
    return header + b"\x00" * (head_len - len(header)) + section


def test_read_pvdi_roundtrip(tmp_path: Path) -> None:
    envelope = bytes([0, 1, 2, 3, 4] * 100)
    twoex = tmp_path / "ANLZ0000.2EX"
    twoex.write_bytes(_synthetic_2ex(envelope))
    result = rb_vendor.read_pvdi(twoex)
    assert result is not None
    fps, decoded = result
    assert round(fps, 2) == 21.53
    assert decoded == envelope


def test_read_pvdi_rejects_changed_fixed_header(tmp_path: Path) -> None:
    twoex = tmp_path / "ANLZ0000.2EX"
    mutated = bytes.fromhex("0000040056220002")  # version bumped
    twoex.write_bytes(_synthetic_2ex(bytes(50), fixed_header=mutated))
    with pytest.raises(ValueError, match="fixed header changed"):
        rb_vendor.read_pvdi(twoex)


def test_vocals_payload_three_states(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere" / "ANLZ0000.2EX"
    assert rb_vendor.vocals_payload(missing) == {"status": "not_analyzed"}

    no_pvdi = tmp_path / "no_pvdi.2EX"
    head = b"PMAI" + struct.pack(">II", 28, 28)
    no_pvdi.write_bytes(head + b"\x00" * (28 - len(head)))
    assert rb_vendor.vocals_payload(no_pvdi) == {"status": "not_analyzed"}

    silent = tmp_path / "silent.2EX"
    silent.write_bytes(_synthetic_2ex(bytes(_frames(30.0))))
    assert rb_vendor.vocals_payload(silent) == {
        "status": "no_vocals", "fps": 21.53, "regions": [],
    }

    vocal = tmp_path / "vocal.2EX"
    vocal.write_bytes(_synthetic_2ex(bytes([3] * _frames(10.0))))
    payload = rb_vendor.vocals_payload(vocal)
    assert payload["status"] == "rekordbox"
    assert payload["fps"] == 21.53
    assert payload["regions"] == [
        {"start_s": 0.0, "end_s": pytest.approx(10.0, abs=0.1), "intensity": 3}
    ]


# ----- vocals row-hydration cache ------------------------------------------------
#
# build_track_rows calls vocals_for_content once per row, and the All Tracks
# pane re-walks the whole library every LIBRARY_FALLBACK_POLL_MS. Without a
# cache that is one full .2EX read plus a PVDI section walk per row per poll.


def _count_2ex_reads(monkeypatch: pytest.MonkeyPatch, target: Path) -> list[int]:
    """Count ``read_bytes`` calls against ``target`` in a one-slot list."""
    calls = [0]
    real_read_bytes = Path.read_bytes

    def counting_read_bytes(self: Path) -> bytes:
        if self == target:
            calls[0] += 1
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    return calls


def _content_for_2ex(monkeypatch: pytest.MonkeyPatch, twoex: Path) -> rb_vendor.RbContent:
    """An RbContent whose AnalysisDataPath resolves to ``twoex``'s sibling.

    ``folder_path`` is None on purpose: that short-circuits the demucs
    fallback, so the only filesystem work left is the PVDI read under test.
    """
    from apps.adapters.rekordbox import paths as rb_paths
    from apps.shared import platform_paths as pp

    monkeypatch.setattr(
        rb_paths,
        "resolve_asset_path",
        lambda original: pp.MappedPath(
            original=original,
            resolved=twoex.with_suffix(".DAT"),
            mapped=True,
            reason="test",
        ),
    )
    monkeypatch.setattr(
        rb_paths,
        "_asset_sibling",
        lambda _mapped, candidate: pp.MappedPath(
            original=str(candidate), resolved=candidate, mapped=True, reason="test"
        ),
    )
    return rb_vendor.RbContent(
        stable_id="track-vocals",
        vendor_id="vendor-1",
        folder_path=None,
        image_path=None,
        analysis_data_path="ANLZ0000.DAT",
        length_s=None,
        comment=None,
        genre=None,
    )


def test_vocals_for_content_reuses_cache_until_mtime_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    twoex = tmp_path / "ANLZ0000.2EX"
    twoex.write_bytes(_synthetic_2ex(bytes([3] * _frames(10.0))))
    content = _content_for_2ex(monkeypatch, twoex)
    rb_anlz._VOCALS_CACHE.clear()
    reads = _count_2ex_reads(monkeypatch, twoex)

    cold = rb_vendor.vocals_for_content(content)
    assert cold["status"] == "rekordbox"
    assert reads[0] == 1, "the cold row must read the .2EX exactly once"

    warm = rb_vendor.vocals_for_content(content)
    assert warm == cold
    assert reads[0] == 1, "a warm row must serve from cache, not re-read the .2EX"

    # Re-analysis rewrites the .2EX: a new mtime must invalidate the entry.
    twoex.write_bytes(_synthetic_2ex(bytes(_frames(30.0))))
    stamp = twoex.stat().st_mtime + 10
    os.utime(twoex, (stamp, stamp))
    reanalyzed = rb_vendor.vocals_for_content(content)
    assert reanalyzed["status"] == "no_vocals", "new mtime must force a re-read"
    assert reads[0] == 2


def test_vocals_payload_cache_hands_out_isolated_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller mutating its row payload must not poison the next caller."""
    twoex = tmp_path / "ANLZ0000.2EX"
    twoex.write_bytes(_synthetic_2ex(bytes([3] * _frames(10.0))))
    rb_anlz._VOCALS_CACHE.clear()

    first = rb_vendor.vocals_payload(twoex)
    first["regions"][0]["intensity"] = 99
    first["status"] = "mutated"

    second = rb_vendor.vocals_payload(twoex)
    assert second["status"] == "rekordbox"
    assert second["regions"][0]["intensity"] == 3


# ----- _peak_downsample_cols -----------------------------------------------------

def test_peak_downsample_is_per_bucket_max() -> None:
    cols = np.zeros((1200, 3), dtype=np.uint8)
    cols[5, 0] = 87   # transient inside bucket 0 must survive (A1: max, not mean)
    cols[1199, 2] = 41
    strip = rb_vendor._peak_downsample_cols(cols, 120)
    assert strip.shape == (120, 3)
    assert strip[0, 0] == 87
    assert strip[119, 2] == 41
    assert strip.sum() == 87 + 41


def test_peak_downsample_rejects_too_few_columns() -> None:
    with pytest.raises(ValueError, match="cannot downsample"):
        rb_vendor._peak_downsample_cols(np.zeros((60, 3), dtype=np.uint8), 120)


# ----- anlz-cache atomic write ---------------------------------------------------

def test_store_cached_payload_roundtrips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rb_config, "ANLZ_CACHE_DIR", tmp_path / "anlz-cache")
    rb_vendor._store_cached_payload("sid", 111.0, 300, {"marker": "v1"})
    assert rb_vendor._load_cached_payload("sid", 111.0, 300) == {"marker": "v1"}
    assert list((tmp_path / "anlz-cache").glob("*.tmp")) == [], (
        "a completed store must not leave .tmp files behind"
    )


def test_interrupted_cache_write_never_truncates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill mid-write -> old entry intact (or absent), never truncated JSON."""
    monkeypatch.setattr(rb_config, "ANLZ_CACHE_DIR", tmp_path / "anlz-cache")
    cache_file = rb_vendor._cache_path("sid")
    real_write_text = Path.write_text

    def truncating_write_text(self: Path, data: str, *args, **kwargs) -> int:
        real_write_text(self, data[: len(data) // 2], *args, **kwargs)
        raise OSError("simulated kill mid-write")

    # No prior entry: an interrupted write must publish nothing at all.
    monkeypatch.setattr(Path, "write_text", truncating_write_text)
    with pytest.raises(OSError, match="simulated kill"):
        rb_vendor._store_cached_payload("sid", 111.0, 300, {"marker": "v1"})
    assert not cache_file.exists()
    assert list((tmp_path / "anlz-cache").glob("*.tmp")) == []
    assert rb_vendor._load_cached_payload("sid", 111.0, 300) is None

    # Prior complete entry, interrupted overwrite: the entry must stay
    # byte-identical and parseable (the old direct write left half a file).
    monkeypatch.setattr(Path, "write_text", real_write_text)
    rb_vendor._store_cached_payload("sid", 111.0, 300, {"marker": "v1"})
    before = cache_file.read_text(encoding="utf-8")
    monkeypatch.setattr(Path, "write_text", truncating_write_text)
    with pytest.raises(OSError, match="simulated kill"):
        rb_vendor._store_cached_payload("sid", 222.0, 300, {"marker": "v2"})
    assert cache_file.read_text(encoding="utf-8") == before
    assert json.loads(before)["payload"] == {"marker": "v1"}
    assert rb_vendor._load_cached_payload("sid", 111.0, 300) == {"marker": "v1"}


def test_store_cached_payload_allows_concurrent_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rb_config, "ANLZ_CACHE_DIR", tmp_path / "anlz-cache")
    real_write_text = Path.write_text
    writers_ready = threading.Barrier(2)

    def synchronized_write_text(self: Path, data: str, *args, **kwargs) -> int:
        written = real_write_text(self, data, *args, **kwargs)
        writers_ready.wait(timeout=5)
        return written

    monkeypatch.setattr(Path, "write_text", synchronized_write_text)
    writes = [(300, {"marker": "v1"}), (600, {"marker": "v2"})]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(rb_vendor._store_cached_payload, "sid", 111.0, points, payload)
            for points, payload in writes
        ]
        errors = [future.exception(timeout=5) for future in futures]

    assert errors == [None, None]
    cache_entry = json.loads(rb_vendor._cache_path("sid").read_text(encoding="utf-8"))
    assert (cache_entry["points"], cache_entry["payload"]) in writes
    assert list((tmp_path / "anlz-cache").glob("*.tmp")) == []


def test_cache_publication_waits_for_concurrent_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Windows reader handle must close before atomic publication starts."""
    monkeypatch.setattr(rb_config, "ANLZ_CACHE_DIR", tmp_path / "anlz-cache")
    rb_vendor._store_cached_payload("sid", 111.0, 300, {"marker": "v1"})
    real_read_text = Path.read_text
    real_write_text = Path.write_text
    reader_opened = threading.Event()
    release_reader = threading.Event()
    writer_staged = threading.Event()

    def blocking_read_text(self: Path, *args, **kwargs) -> str:
        with self.open("r", encoding="utf-8") as stream:
            reader_opened.set()
            assert release_reader.wait(timeout=5)
            return stream.read()

    def observed_write_text(self: Path, data: str, *args, **kwargs) -> int:
        written = real_write_text(self, data, *args, **kwargs)
        writer_staged.set()
        return written

    monkeypatch.setattr(Path, "read_text", blocking_read_text)
    monkeypatch.setattr(Path, "write_text", observed_write_text)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(rb_vendor._load_cached_payload, "sid", 111.0, 300)
        assert reader_opened.wait(timeout=5)
        writer = pool.submit(
            rb_vendor._store_cached_payload,
            "sid",
            222.0,
            600,
            {"marker": "v2"},
        )
        assert writer_staged.wait(timeout=5)
        assert not writer.done(), "publication raced an open cache reader"
        release_reader.set()

    assert reader.result(timeout=5) == {"marker": "v1"}
    assert writer.exception(timeout=5) is None
    monkeypatch.setattr(Path, "read_text", real_read_text)
    assert rb_vendor._load_cached_payload("sid", 222.0, 600) == {"marker": "v2"}


# ----- keep_by_availability ------------------------------------------------------

@pytest.mark.parametrize(
    ("available", "file_exists", "expected"),
    [
        ("all", True, True), ("all", False, True),
        ("true", True, True), ("true", False, False),
        ("false", True, False), ("false", False, True),
    ],
)
def test_keep_by_availability(available, file_exists, expected) -> None:
    assert keep_by_availability(available, file_exists) is expected


def test_bulk_file_exists_treats_dataless_stub_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Darwin sparse stubs must not pass the FR-1 file_exists gate."""
    import os
    import sys

    from apps.shared import fs_residency
    from apps.shared import platform_paths as pp

    # Darwin first: st_blocks is a POSIX-only stat field that Windows does
    # not carry at all, so reading it to decide whether to skip crashed the
    # very platforms the skip existed for.
    if sys.platform != "darwin":
        pytest.skip("dataless gate is Darwin-scoped")

    stub = tmp_path / "stub.mp3"
    with open(stub, "wb") as handle:
        handle.truncate(2_000_000)
    if not fs_residency.is_dataless_stub(os.stat(stub)):
        pytest.skip("filesystem does not support sparse files; cannot mimic a placeholder")

    # Bypass path-map resolution: treat the absolute path as already local.
    monkeypatch.setattr(
        rb_vendor,
        "resolve_asset_path",
        lambda path: pp.MappedPath(
            original=path, resolved=Path(path), mapped=False, reason="test",
        ),
    )
    rb_config._FILE_EXISTS_CACHE.clear()
    assert rb_vendor.bulk_file_exists([str(stub)]) == {str(stub): False}
    assert rb_vendor.bulk_file_size([str(stub)]) == {str(stub): None}
