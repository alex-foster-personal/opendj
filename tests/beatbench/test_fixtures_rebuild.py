"""The rebuild path's checksum guard, mutated in both directions.

The guard exists because the excerpt WAVs are gitignored scratch and have to be
re-decoded every round. If a decoder, an ffmpeg upgrade or the source audio
changes between rounds, the rebuilt excerpt is NOT the fixture the previous
round measured, and every delta computed against it is unattributable.

Round 1 shipped this the wrong way round: `rebuild_one` overwrote the recorded
`wav_sha256` with the freshly decoded file's digest, which adopts any drift as
the new baseline and is a check that cannot fail. Codex caught it on PR #1514.
These tests pin both directions so it cannot regress:

  - a digest that MATCHES is accepted, and
  - a digest that DIFFERS drops the fixture by name.

The second is the one that matters, and it is written by deliberately changing
the recorded digest rather than by trusting the code path to be reachable.

NO STUBS, NO MONKEYPATCHING, AND THE REASON IS NOT STYLE. The first version of
this file replaced `decode_excerpt` and `fetch_reference_beats` with functions
that wrote a WAV and returned a beat list, which meant the suite would stay
green even if the real ffmpeg invocation stopped producing the bytes the guard
is supposed to be hashing, or the real `/anlz` parse stopped producing the grid
it compares. That is a test of the guard's arithmetic while the thing being
guarded is out of scope, and repository policy (AGENTS.md) prohibits it
outright (Codex P1 BLOCKING on PR #1514).

So the daemon is replaced by a REAL HTTP SERVER, not by a replacement for the
functions that call it. `http.server` from the standard library serves two real
payloads on a real socket: the bytes of a real WAV written to disk, with byte
ranges, and a real `/anlz` JSON document in the shape the route returns. Every
line of production code then runs unchanged -- ffmpeg genuinely fetches over
HTTP, genuinely seeks, genuinely resamples to mono 44100, genuinely reports
`volumedetect` levels that the non-silence rule reads; `fetch_reference_beats`
genuinely opens a URL and parses the response. AGENTS.md permits captured
payloads and real media as INPUTS as long as they are consumed through the
production decoder and API paths, which is exactly this shape.

What that buys, concretely: if `decode_excerpt`'s ffmpeg arguments were broken
so that it wrote nothing, or the `/anlz` parse stopped filtering to the window,
these tests would fail. Under the stubs they could not have.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import struct
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.beatbench import fixtures as fx

STABLE_ID = "abc123"
SOURCE_S = 180.0
WINDOW_START_S = 75.0
CLICK_BPM = 120.0

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="the rebuild path decodes with the real ffmpeg; without it nothing is measured",
)


# ----- Real inputs --------------------------------------------------------


def _write_source_wav(path: str) -> None:
    """A real, audible 44100 Hz WAV: a decaying tone burst on every beat.

    Not silence, because `decode_excerpt` enforces a peak above -40 dBFS and a
    silent source would exercise the drop path instead of the guard.
    """
    period = 60.0 / CLICK_BPM
    n_samples = int(SOURCE_S * fx.SAMPLE_RATE)
    samples = [0.0] * n_samples
    burst = int(0.04 * fx.SAMPLE_RATE)
    beat = 0
    while beat * period < SOURCE_S:
        start = int(beat * period * fx.SAMPLE_RATE)
        for i in range(burst):
            if start + i >= n_samples:
                break
            envelope = math.exp(-i / (0.010 * fx.SAMPLE_RATE))
            samples[start + i] += 0.8 * envelope * math.sin(2 * math.pi * 1200.0 * i / 44100.0)
        beat += 1
    with wave.open(path, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(fx.SAMPLE_RATE)
        fh.writeframes(
            b"".join(struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32000)) for s in samples)
        )


def _anlz_document() -> dict:
    """An `/anlz` response in the shape `routes/rb_assets.py` returns."""
    period = 60.0 / CLICK_BPM
    return {
        "stable_id": STABLE_ID,
        "beatgrid": {
            "beat_count": int(SOURCE_S / period),
            "beats": [
                {"n": i % 4 + 1, "t": round(i * period, 4), "bpm": CLICK_BPM}
                for i in range(int(SOURCE_S / period))
            ],
        },
    }


def _sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# ----- A real daemon-shaped HTTP server -----------------------------------


def _make_handler(audio_path: str, anlz: bytes):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # keep pytest output readable
            pass

        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self) -> None:  # BaseHTTPRequestHandler dispatches on this name
            self.do_GET()

        def do_GET(self) -> None:  # BaseHTTPRequestHandler dispatches on this name
            if "/anlz" in self.path:
                self._send(anlz, "application/json")
                return
            if not self.path.endswith("/audio"):
                self._send(b"not found", "text/plain", status=404)
                return
            with open(audio_path, "rb") as fh:
                data = fh.read()
            # Real byte ranges, because ffmpeg seeks rather than reading 180
            # seconds of audio to reach second 75.
            span = self.headers.get("Range")
            if not span or not span.startswith("bytes="):
                self._send(data, "audio/wav")
                return
            first, _, last = span[len("bytes="):].partition("-")
            start = int(first or 0)
            end = int(last) if last else len(data) - 1
            chunk = data[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.end_headers()
            self.wfile.write(chunk)

    return Handler


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    """A real HTTP server on a real port, serving real bytes. Yields its base URL."""
    workdir = tmp_path_factory.mktemp("rebuild-source")
    audio_path = str(workdir / "source.wav")
    _write_source_wav(audio_path)

    anlz = json.dumps(_anlz_document()).encode("utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(audio_path, anlz))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def rebuilt(tmp_path, daemon):
    """Drive the REAL `rebuild_one` against the real server. Nothing is patched."""
    wav_dir = tmp_path / "wav"
    wav_dir.mkdir()
    window_end_s = WINDOW_START_S + fx.EXCERPT_S
    ref_beats = fx.fetch_reference_beats(daemon, STABLE_ID, WINDOW_START_S, window_end_s)
    assert len(ref_beats) > 8, "the served grid must reach the window, or nothing is compared"

    def run(recorded_sha: str | None):
        fixture = {
            "stable_id": STABLE_ID,
            "duration_ms": int(SOURCE_S * 1000),
            "window_start_s": WINDOW_START_S,
            "window_end_s": window_end_s,
            "score_start_s": WINDOW_START_S + fx.GUARD_S,
            "score_end_s": window_end_s - fx.GUARD_S,
            "is_dynamic": False,
            "rb_bpm": CLICK_BPM,
            "grid_beat_count": len(ref_beats),
            "grid_bpm_span": 0.0,
            "ref_beats": ref_beats,
            "wav": str(wav_dir / f"{STABLE_ID}.wav"),
        }
        if recorded_sha is not None:
            fixture["wav_sha256"] = recorded_sha
        return fx.rebuild_one(daemon, fixture, str(wav_dir))

    return run


# ----- The guard, both directions -----------------------------------------


def test_a_matching_digest_is_accepted(rebuilt):
    """The positive direction. Establishes the guard is reachable at all.

    It also establishes that the whole production chain worked: a real ffmpeg
    fetched real audio over HTTP, seeked to the recorded window, and wrote a
    real excerpt whose peak cleared the non-silence rule. Under the previous
    stubs none of that was exercised.
    """
    first = rebuilt(None)
    assert first["ok"] is True, first.get("reason")
    assert "wav_sha256" in first
    assert first["wav_bytes"] > 1024
    assert first["rebuilt_max_volume_dbfs"] > fx.MIN_PEAK_DBFS
    assert os.path.exists(first["wav"])

    again = rebuilt(first["wav_sha256"])
    assert again["ok"] is True, again.get("reason")
    assert again["wav_sha256"] == first["wav_sha256"], (
        "the real decoder must be deterministic, or the guard would fire on itself"
    )


def test_a_changed_digest_drops_the_fixture_by_name(rebuilt):
    """The direction that failed before the fix: drift must NOT be adopted.

    A wrong recorded digest stands in for the real cause (a different ffmpeg, a
    re-encoded source). The fixture must be dropped with a reason naming both
    digests, never silently rewritten to the new value.
    """
    drifted = rebuilt("0" * 64)

    assert drifted["ok"] is False
    assert drifted["stable_id"] == STABLE_ID
    assert "excerpt bytes changed" in drifted["reason"]
    assert "0000000000000000" in drifted["reason"]
    assert "wav_sha256" not in drifted, "a dropped fixture must not carry a rewritten digest"


def test_a_dropped_fixture_leaves_no_wav_behind(rebuilt, tmp_path):
    """A rejected excerpt on disk would be scored by a later run as if it passed."""
    first = rebuilt(None)
    assert first["ok"] is True, first.get("reason")
    assert os.path.exists(first["wav"])

    drifted = rebuilt("0" * 64)

    assert drifted["ok"] is False
    assert not os.path.exists(first["wav"])


def test_the_first_rebuild_establishes_a_baseline_rather_than_failing(rebuilt):
    """Round 0 recorded no checksums, so a fixture without one must still pass."""
    first = rebuilt(None)

    assert first["ok"] is True, first.get("reason")
    assert first["wav_sha256"] == _sha256(first["wav"])


# ----- Controls on the instrument itself ----------------------------------


def test_the_served_grid_is_filtered_to_the_window(daemon):
    """A negative control: beats outside the window must NOT come back.

    If `fetch_reference_beats` stopped filtering, every window would compare
    against the whole track's grid and the drift check above would be
    comparing the wrong thing while still looking green.
    """
    window_end_s = WINDOW_START_S + fx.EXCERPT_S
    inside = fx.fetch_reference_beats(daemon, STABLE_ID, WINDOW_START_S, window_end_s)
    whole = fx.fetch_reference_beats(daemon, STABLE_ID, 0.0, SOURCE_S)

    assert len(inside) < len(whole)
    assert all(WINDOW_START_S <= t < window_end_s for _, t, _ in inside)


def test_an_empty_window_returns_nothing_rather_than_everything(daemon):
    """The other half of the control: a window with no beats must read as none."""
    assert fx.fetch_reference_beats(daemon, STABLE_ID, SOURCE_S + 10, SOURCE_S + 20) == []
