"""perf-lab media.mjs against the real engine: `apps.engine_core serve` booted
as a perf-lab stack boots it (stack.stack_env, --data-dir) over a disposable
data dir whose migrated state.db holds the checked-in conformance mp3 (the
fixture tests/webui/test_audio_deck_load_contract.py uses). No configuration
is patched: the engine finds its databases through its own startup path. The
digests must be of the bytes the production /audio, /anlz and /artwork routes
serve, and a required asset the engine cannot serve must be an error, never a
silent gap."""

from __future__ import annotations

import contextlib
import hashlib
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.perf.perflab_support import LAB, REPO, _load, _node_json
from tests.webui.test_audio_deck_load_contract import (
    GONE_SID,
    PLAYABLE_SID,
    REAL_AUDIO_FIXTURE,
    _insert_track,
    _verify_fixture_checksum,
)

stack = _load("stack")

_MEDIA = """
const [url, base, ...rest] = process.argv.slice(1);
const { mediaDigests, isTrackAsset } = await import(url);
const sep = rest.indexOf('--');
const ids = rest.slice(0, sep), observed = rest.slice(sep + 1);
const out = await mediaDigests(base, ids, observed);
out.asset = ['/api/v1/tracks/x/audio', '/api/v1/usb/tracks/usb-x/anlz', '/api/v1/tracks/x/artwork',
  '/api/v1/tracks/x/stems/vocals', '/api/v1/tracks/x/stems/state', '/api/v1/tracks/x/rb-meta'].map(isTrackAsset);
console.log(JSON.stringify(out));
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def _serving(tmp_path: Path, name: str = "data") -> Iterator[str]:
    """A real engine over tmp_path/<name>, whose state.db holds the playable
    and the moved-away conformance tracks; yields its base URL."""
    _verify_fixture_checksum()
    data = tmp_path / name
    state_path = data / "state" / "state.db"
    state_path.parent.mkdir(parents=True)
    _insert_track(state_path, PLAYABLE_SID, str(REAL_AUDIO_FIXTURE))
    _insert_track(state_path, GONE_SID, str(tmp_path / "moved-away.mp3"))
    port = _free_port()
    argv = [sys.executable, "-m", "apps.engine_core", "serve", "--data-dir", str(data)]
    log = tmp_path / f"{name}-engine.log"
    with open(log, "w", encoding="utf-8") as out:
        proc = subprocess.Popen(
            [*argv, "--host", "127.0.0.1", "--port", str(port)],
            cwd=REPO,
            env=stack.stack_env(REPO, data, os.environ),
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(f"{base}/api/v1/health", timeout=2):
                    break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    pytest.fail(f"the engine did not start serving: {log.read_text(encoding='utf-8')[-2000:]}")
                time.sleep(0.2)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[str]:
    with _serving(tmp_path) as base:
        yield base


def _served(url: str) -> str:
    """sha256 of status + body as the engine answers it, read by Python; a
    non-2xx answer is its status alone (its body is a diagnostic)."""
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            status, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, b""
    return hashlib.sha256(f"{status}\n".encode() + body).hexdigest()


def test_media_digests_hash_what_the_production_routes_serve(engine: str) -> None:
    audio = f"/api/v1/tracks/{PLAYABLE_SID}/audio"
    anlz = f"/api/v1/tracks/{PLAYABLE_SID}/anlz"
    art = f"/api/v1/tracks/{PLAYABLE_SID}/artwork?size=orig"
    out = _node_json(_MEDIA, (LAB / "media.mjs").as_uri(), engine, PLAYABLE_SID, GONE_SID, "--", art)
    want_audio = hashlib.sha256(b"200\n" + REAL_AUDIO_FIXTURE.read_bytes()).hexdigest()
    # the audio digest is the checked-in file's bytes as /audio serves them
    assert out["digests"][audio] == want_audio == _served(engine + audio), out
    assert out["digests"][anlz] == _served(engine + anlz)
    # an observed asset is hashed whatever it answers (no artwork is its identity)
    assert out["digests"][art] == _served(engine + art)
    # the moved-away track: its required audio cannot be served, so no entry and a named error
    gone = f"/api/v1/tracks/{GONE_SID}/audio"
    assert gone not in out["digests"]
    assert any(e.startswith(f"cannot hash {gone}: HTTP ") for e in out["errors"]), out["errors"]
    assert out["asset"] == [True, True, True, True, False, False]


_CHANGES = """
const [url, base, id, file] = process.argv.slice(1);
const fs = await import('node:fs');
const { mediaChanges, mediaDigests } = await import(url);
const art = `/api/v1/tracks/${id}/artwork?size=orig`;
const first = await mediaDigests(base, [id], [art]);
const again = await mediaDigests(base, [id], [art]);
fs.appendFileSync(file, Buffer.from('replaced'));
const after = await mediaDigests(base, [id], [art]);
console.log(JSON.stringify({
  errors: [...first.errors, ...again.errors, ...after.errors],
  unchanged: mediaChanges(first.digests, again.digests),
  replaced: mediaChanges(first.digests, after.digests),
  only_new: mediaChanges({}, after.digests),
}));
"""


def test_a_library_file_replaced_between_two_digests_is_named(engine: str, tmp_path: Path) -> None:
    """The A/B hashes each side's inputs around its windows (abwin.mjs): the
    real engine serving a library file outside the data dir, replaced in place
    between two digests, names exactly that file's /audio; two digests of the
    same bytes name nothing (control), and an asset first seen later is not a
    change."""
    library = tmp_path / "library.mp3"
    library.write_bytes(REAL_AUDIO_FIXTURE.read_bytes())
    _insert_track(tmp_path / "data" / "state" / "state.db", "replaced-sid", str(library))
    out = _node_json(_CHANGES, (LAB / "media.mjs").as_uri(), engine, "replaced-sid", str(library))
    assert out == {
        "errors": [],
        "unchanged": [],
        "replaced": ["/api/v1/tracks/replaced-sid/audio"],
        "only_new": [],
    }, out


_SIDES = """
const [url, a, b, id] = process.argv.slice(1);
const { mediaDigests, mediaSidesDiffer } = await import(url);
const art = `/api/v1/tracks/${id}/artwork?size=orig`;
const A = await mediaDigests(a, [id], [art]);
const B = await mediaDigests(b, [id], [art]);
const { [art]: _dropped, ...bWithoutArt } = B.digests;
console.log(JSON.stringify({
  errors: [...A.errors, ...B.errors],
  same_side: mediaSidesDiffer(A.digests, A.digests),
  sides: mediaSidesDiffer(A.digests, B.digests),
  a_only: mediaSidesDiffer(A.digests, bWithoutArt),
  b_only: mediaSidesDiffer(bWithoutArt, A.digests),
}));
"""


def test_two_sides_serving_different_bytes_for_one_id_are_named(tmp_path: Path) -> None:
    """Two real engines, as an A/B's base and fix stacks, each resolving the
    same stable id to its own library file outside its data dir. Identical
    bytes on both sides name nothing (control); B's file differing from the
    outset names exactly that id's /audio, although neither side ever
    changes; and an asset hashed on one side only is named by its side."""
    lib_a, lib_b = tmp_path / "lib-a.mp3", tmp_path / "lib-b.mp3"
    for lib in (lib_a, lib_b):
        lib.write_bytes(REAL_AUDIO_FIXTURE.read_bytes())
    with _serving(tmp_path, "data-a") as a, _serving(tmp_path, "data-b") as b:
        _insert_track(tmp_path / "data-a" / "state" / "state.db", "shared-sid", str(lib_a))
        _insert_track(tmp_path / "data-b" / "state" / "state.db", "shared-sid", str(lib_b))
        same = _node_json(_SIDES, (LAB / "media.mjs").as_uri(), a, b, "shared-sid")
        art = "/api/v1/tracks/shared-sid/artwork?size=orig"
        assert same == {
            "errors": [],
            "same_side": [],
            "sides": [],
            "a_only": [f"{art} was hashed on A only"],
            "b_only": [f"{art} was hashed on B only"],
        }, same
        with lib_b.open("ab") as fh:
            fh.write(b"another master")
        differ = _node_json(_SIDES, (LAB / "media.mjs").as_uri(), a, b, "shared-sid")
    assert differ["errors"] == [] and differ["same_side"] == [], differ
    assert differ["sides"] == ["/api/v1/tracks/shared-sid/audio differs between A and B"], differ


def test_the_driver_hashes_its_inputs_before_measuring_and_voids_a_change() -> None:
    """A capture hashes every fixture id's audio and /anlz before any measured
    phase and again at the end; mediaChanges (tested against the real engine
    above) between the two, or an input it could not hash, voids the capture."""
    src = (LAB / "driver.mjs").read_text(encoding="utf-8")
    run = src[src.index("await waitReady();") :]
    assert run.index("mediaBefore = await mediaDigests(opts.url, opts.ids, [...assetRefs]);") < run.index(
        "await startTrace();"
    )
    tail = src[src.index("const media = await mediaDigests(opts.url, opts.ids, [...assetRefs]);") :]
    assert "mediaChanges(before.digests, media.digests)" in tail
    assert "for (const why of [...before.errors, ...media.errors, ...changed, ...unbased])" in tail


def test_every_asset_is_baselined_by_an_unmeasured_warm_up_never_inside_a_phase() -> None:
    """A throwaway browser context loads and plays the measured decks before
    the measured page exists, so artwork and stems are requested, and hashed,
    before the trace starts. The request handler only records refs: a digest
    fetch alongside a phase would charge the probe's own work to it. An asset
    first requested inside a phase has no baseline and voids the capture."""
    src = (LAB / "driver.mjs").read_text(encoding="utf-8")
    record = src[src.index("const recordAsset = (r) => {") :]
    record = record[: record.index("};")]
    assert "assetRefs.add(u.pathname + u.search);" in record
    assert "mediaDigests(" not in record and "fetch(" not in record
    warm = src[src.index("async function warmUp() {") : src.index("async function scenarioSteady() {")]
    assert "const ctx = await browser.newContext(" in warm and "wp.on('request', recordAsset);" in warm
    assert (
        warm.index("type: 'load'")
        < warm.index("type: 'play', deck: d, playing: true")
        < warm.index("await ctx.close();")
    )
    assert "throw new Error(`warm-up decks did not load:" in warm
    # every id a scenario can load (the leak cycles rotate through all of them), not only the first four
    assert "for (let i = 0; i < opts.ids.length; i += decks) await warmBatch(opts.ids.slice(i, i + decks));" in warm
    assert "stable_id: ids[d - 1]" in warm and "loadedDecksFaults(wp, ids)" in warm
    assert "stable_id: opts.ids[" not in warm
    run = src[src.index("await warmUp();") :]
    assert (
        run.index("await waitReady();")
        < run.index("mediaBefore = await mediaDigests(opts.url, opts.ids, [...assetRefs]);")
        < run.index("await startTrace();")
    )
    tail = src[src.index("const media = await mediaDigests(opts.url, opts.ids, [...assetRefs]);") :]
    assert "filter((ref) => !(ref in before.digests))" in tail
    assert "was first requested inside a measured phase, with no baseline digest" in tail
    assert "meta.invalid ??= why;" in tail
