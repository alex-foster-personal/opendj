"""Waveform input pinning is all or nothing, and swaps alone are retried (Codex on #6820, #6821)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from apps.stems.artifacts import StemArtifactError, load_stem_bundle
from apps.stems.bundle_lock import bundle_lock, replace_bundle_dir
from apps.stems.stem_waveform import StemPartChangedError, build_stem_waveform_payload, pin_waveform_inputs
from apps.webui.server.routes import stems as stems_routes
from tests.webui.test_stems_hydration import _client, _manifest_bytes, _wav_bytes

SID = "pinned-track"


def _write_bundle(directory: Path, *, frames: int = 8) -> None:
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_bytes(_manifest_bytes(SID))
    for part in ("vocals", "drums", "bass", "other"):
        (directory / f"{part}.wav").write_bytes(_wav_bytes(frames=frames))


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    stems_dir = data_dir / "state" / "stems"
    _write_bundle(stems_dir / SID)
    return data_dir, stems_dir


def _pin_dirs(data_dir: Path) -> list[Path]:
    return sorted((data_dir / "state" / ".stem-publish").glob("waveform-pin-*"))


def test_one_unlinkable_input_pins_none_of_them(tmp_path: Path) -> None:
    """[if] bass links but other cannot [then] nothing is pinned and the scratch is gone, [else stop].

    A partial pin let a swap pair the old pinned bass with the new canonical
    other. Here other is replaced by a directory after the bundle resolves,
    which no filesystem hard-links. Control: with both inputs linkable, both
    are pinned.

    MUTATION TARGET: in ``pin_waveform_inputs`` keep going past a failed link.
    """
    data_dir, stems_dir = _layout(tmp_path)
    bundle = load_stem_bundle(SID, stems_dir=stems_dir)

    scratch, pinned, seen = pin_waveform_inputs(bundle, "instrumental")
    assert scratch is not None and set(pinned) == {bundle.files["bass"], bundle.files["other"]}
    assert set(seen) == set(pinned)
    shutil.rmtree(scratch)

    other = bundle.files["other"]
    other.unlink()
    other.mkdir()
    scratch, pinned, seen = pin_waveform_inputs(bundle, "instrumental")
    assert scratch is None
    assert pinned == {}
    assert set(seen) == {bundle.files["bass"], other}
    assert _pin_dirs(data_dir) == []


def test_an_unpinned_input_swapped_after_resolve_is_refused(tmp_path: Path) -> None:
    """[if] no input is pinned and a swap lands after the barrier [then] the build refuses, [else stop].

    Control: the same build with nothing swapped returns the envelope.

    MUTATION TARGET: ignore ``seen`` in ``build_stem_waveform_payload``.
    """
    data_dir, stems_dir = _layout(tmp_path)
    bundle = load_stem_bundle(SID, stems_dir=stems_dir)
    _, _, seen = pin_waveform_inputs(bundle, "instrumental")
    for scratch in _pin_dirs(data_dir):
        shutil.rmtree(scratch)

    payload = build_stem_waveform_payload(bundle, "instrumental", pinned={}, seen=seen)
    assert payload["envelope"], "control: an untouched bundle builds"

    staging = data_dir / "state" / ".stem-publish" / f"{SID}-next"
    _write_bundle(staging, frames=16)
    with bundle_lock(stems_dir / SID):
        replace_bundle_dir(staging, stems_dir / SID)
    with pytest.raises(StemPartChangedError):
        build_stem_waveform_payload(bundle, "instrumental", pinned={}, seen=seen)


def _request(app: FastAPI) -> Request:
    """A real request bound to the real app, for calling the route's own helpers."""
    return Request({"type": "http", "app": app, "method": "GET", "path": "/", "headers": []})


def test_an_artifact_error_with_no_swap_is_tried_once(tmp_path: Path) -> None:
    """[if] resolving a waveform fails with no swap [then] the route answers after one attempt, [else stop].

    Retrying every artifact error repeated a failing decode, or one that hit
    its timeout, four times. A part the bundle does not have raises the same
    ``StemArtifactError`` with no swap anywhere. The attempt is the route's
    own resolve and build, counted where the test calls it. Control: the
    failure reaches the client as the route's 404, and a real part is served
    after exactly one attempt too.

    MUTATION TARGET: catch ``StemArtifactError`` in ``_retry_swaps``.
    """
    data_dir, stems_dir = _layout(tmp_path)
    with _client(stems_dir, data_dir=data_dir) as client:
        assert isinstance(client.app, FastAPI)
        client.app.state.stem_cache_data_dir = data_dir
        client.app.state.data_dir = data_dir
        request = _request(client.app)
        calls: list[str] = []

        def attempt(part: str) -> dict[str, object]:
            calls.append(part)
            resolved = stems_routes._resolve_waveform_inputs(SID, part, request)
            return stems_routes._build_resolved_waveform(resolved, part, request)

        with pytest.raises(StemArtifactError):
            stems_routes._retry_swaps(lambda: attempt("not-a-part"))
        assert calls == ["not-a-part"]
        assert stems_routes._retry_swaps(lambda: attempt("vocals"))["envelope"]
        assert calls == ["not-a-part", "vocals"]
        missing = client.get(f"/api/v1/tracks/{SID}/stems/not-a-part/waveform")
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["code"] == "STEM_PART_NOT_FOUND"


def _tone_bundle(directory: Path, amplitude: int) -> None:
    _write_bundle(directory, frames=4096)
    for part in ("bass", "other"):
        (directory / f"{part}.wav").write_bytes(_tone_wav(4096, amplitude))


@pytest.mark.parametrize("pinnable", [False, True])
def test_an_unpinned_envelope_is_built_before_the_read_barrier_lets_a_swap_in(tmp_path: Path, pinnable: bool) -> None:
    """[if] the parts cannot be pinned [then] the envelope is built under the read barrier, [else stop].

    Decoding the canonical paths after the barrier, a swap and its rollback
    (which puts the same inodes back) could land between the bass and other
    decodes with every stat unchanged, so a mixed envelope would pass the
    identity check and be cached (Sol on #6820). Pinning fails for real:
    ``.stem-publish`` is a plain file, so the production
    ``pin_waveform_inputs`` cannot create its link directory, as on a volume
    that refuses it. A real writer swaps in a bundle that decodes
    differently the moment the route's resolve returns, which is when its
    barrier is released. Built under the barrier, the answer is the
    original bundle's envelope after one attempt; built after it, the swap
    is caught, retried and the new one served. Control: where the parts are
    pinned, the decode runs after the barrier and reads the pinned original.

    MUTATION TARGET: build the unpinned envelope in ``_build_resolved_waveform``.
    """
    import shutil as _shutil

    data_dir = tmp_path / "data"
    stems_dir = data_dir / "state" / "stems"
    _tone_bundle(stems_dir / SID, 1000)
    original = build_stem_waveform_payload(load_stem_bundle(SID, stems_dir=stems_dir), "instrumental")
    publish_dir = data_dir / "state" / ".stem-publish"
    if pinnable:
        staging = publish_dir / f"{SID}-next"
    else:
        publish_dir.write_bytes(b"")
        staging = tmp_path / "staging" / SID
    _tone_bundle(staging, 3000)
    newer = tmp_path / "copy" / SID
    _shutil.copytree(staging, newer)
    swapped_envelope = build_stem_waveform_payload(load_stem_bundle(SID, stems_dir=newer.parent), "instrumental")
    assert swapped_envelope["envelope"] != original["envelope"], "control: the generations decode differently"

    with _client(stems_dir, data_dir=data_dir) as client:
        assert isinstance(client.app, FastAPI)
        client.app.state.stem_cache_data_dir = data_dir
        client.app.state.data_dir = data_dir
        request = _request(client.app)
        pins: list[dict[Path, Path]] = []

        def attempt() -> dict[str, object]:
            resolved = stems_routes._resolve_waveform_inputs(SID, "instrumental", request)
            pins.append(resolved[2])
            if len(pins) == 1:
                with bundle_lock(stems_dir / SID):
                    replace_bundle_dir(staging, stems_dir / SID)
            return stems_routes._build_resolved_waveform(resolved, "instrumental", request)

        payload = stems_routes._retry_swaps(attempt)
    assert len(pins) == 1, "one attempt: nothing the decode read was swapped under it"
    assert bool(pins[0]) is pinnable, "control: the real pin step pinned as the volume allows"
    assert payload["envelope"] == original["envelope"]
    assert load_stem_bundle(SID, stems_dir=stems_dir).files["bass"].read_bytes() == (newer / "bass.wav").read_bytes(), (
        "the writer's swap landed"
    )


def _tone_wav(frames: int, amplitude: int) -> bytes:
    """A 16-bit stereo WAV the size of ``_wav_bytes(frames=frames)``, whatever ``amplitude`` is."""
    import io
    import struct
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(44_100)
        out.writeframes(b"".join(struct.pack("<hh", amplitude * (i % 7 - 3), 0) for i in range(frames)))
    return buf.getvalue()


def test_a_lookalike_swapped_in_with_the_same_size_and_mtime_is_decoded_not_served_from_cache(tmp_path: Path) -> None:
    """[if] a swap leaves a part's size and mtime as they were [then] the cache misses and the new audio is decoded, [else stop].

    On a coarse clock (1-2 s on HFS+ or FAT) a re-render can keep both, and
    a key of path, mtime and size served the old render's envelope (Sol on
    #6820). Every bundle writer swaps a directory in, so the part's inode is
    new. Control: the first build is a real cache entry, served again while
    nothing changes.

    MUTATION TARGET: drop ``st_dev`` and ``st_ino`` from ``_file_fingerprint``.
    """
    import os

    data_dir = tmp_path / "data"
    stems_dir = data_dir / "state" / "stems"
    _write_bundle(stems_dir / SID, frames=4096)
    cache = tmp_path / "cache"
    bundle = load_stem_bundle(SID, stems_dir=stems_dir)
    first = build_stem_waveform_payload(bundle, "vocals", cache_root=cache)
    assert build_stem_waveform_payload(bundle, "vocals", cache_root=cache) == first
    assert any(cache.rglob("vocals-*.json")), "control: the first build was cached"

    before = (stems_dir / SID / "vocals.wav").stat()
    staging = data_dir / "state" / ".stem-publish" / f"{SID}-next"
    _write_bundle(staging, frames=4096)
    (staging / "vocals.wav").write_bytes(_tone_wav(4096, 3000))
    os.utime(staging / "vocals.wav", ns=(before.st_atime_ns, before.st_mtime_ns))
    with bundle_lock(stems_dir / SID):
        replace_bundle_dir(staging, stems_dir / SID)
    after = (stems_dir / SID / "vocals.wav").stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)

    swapped = load_stem_bundle(SID, stems_dir=stems_dir)
    fresh = build_stem_waveform_payload(swapped, "vocals")
    assert fresh["envelope"] != first["envelope"], "control: the generations decode differently"
    assert build_stem_waveform_payload(swapped, "vocals", cache_root=cache) == fresh


def test_a_pin_directory_that_cannot_be_removed_is_reported_by_name(tmp_path: Path) -> None:
    """[if] removing a pin directory fails [then] ``StemPinCleanupError`` names it, [else stop].

    ``ignore_errors`` left the scratch and its hard links behind in silence,
    keeping the old stem bytes on disk (Sol on #6820). A path that is a file
    is a real ``rmtree`` failure for every user, root included. Control: a
    directory already gone is not an error.

    MUTATION TARGET: ``shutil.rmtree(scratch, ignore_errors=True)`` in
    ``remove_waveform_pins`` (the pre-fix cleanup).
    """
    from apps.stems.stem_waveform import StemPinCleanupError, remove_waveform_pins

    stuck = tmp_path / "waveform-pin-stuck"
    stuck.write_bytes(b"not a directory")
    with pytest.raises(StemPinCleanupError, match="waveform-pin-stuck") as raised:
        remove_waveform_pins(stuck)
    assert raised.value.scratch == stuck
    remove_waveform_pins(tmp_path / "waveform-pin-gone")


def test_a_stale_pin_directory_is_swept_by_the_next_pin_and_a_fresh_one_is_kept(tmp_path: Path) -> None:
    """[if] a pin directory older than ``WAVEFORM_PIN_STALE_S`` is left [then] the next pin removes it, [else stop].

    A failed removal, or a process that died mid-decode, leaves a pin no
    caller will ever remove (Sol on #6820). Control: a pin younger than the
    limit, which may be another request's decode in flight, is kept.

    MUTATION TARGET: drop the ``sweep_stale_waveform_pins`` call from
    ``pin_waveform_inputs``.
    """
    import os
    import time

    from apps.stems.stem_waveform import WAVEFORM_PIN_STALE_S, remove_waveform_pins

    data_dir, stems_dir = _layout(tmp_path)
    publish = data_dir / "state" / ".stem-publish"
    stale, fresh = publish / "waveform-pin-stale", publish / "waveform-pin-fresh"
    for directory in (stale, fresh):
        directory.mkdir(parents=True)
        (directory / "0-vocals.wav").write_bytes(b"old bytes")
    old = time.time() - WAVEFORM_PIN_STALE_S - 60
    os.utime(stale, (old, old))

    scratch, pinned, _ = pin_waveform_inputs(load_stem_bundle(SID, stems_dir=stems_dir), "vocals")
    assert scratch is not None and pinned
    assert not stale.exists(), "the stale pin was not swept"
    assert fresh.exists(), "a pin in flight was swept"
    remove_waveform_pins(scratch)
    assert _pin_dirs(data_dir) == [fresh]
