"""scripts/stem_bundle_worker.py refuses to stem a truncated file (issue #6150 B3).

[if] a stems source decodes under half its claimed length [then] no bundle is written and the job names the file, [else stop].

The installed app decodes non-WAV sources through ``odj-audio`` into a temp
WAV and read the SOURCE duration from that WAV, so ``_guard_duration``
compared the decode with itself. The acceptance probe's 4096-byte cut of a
2 s mp3 decoded to 0.26 s, agreed with itself, and was written as a stem
bundle that readiness then counted as ``stems_ready``. ``_guard_truncated``
compares the decode with the container's own claim instead. Run without
torch, like the sibling decode tests: the guard is a pure decision.

[if] the decode is far shorter than the file claims [then ⛔️] RuntimeError
     naming the file, before any separation, so the per-track failure lands
     in the job's error and no bundle is written
[if] the uncut short carrier [then] the guard passes
[if] the claimed length cannot be read [then ⛔️] RuntimeError naming the file
     (stems fail closed: a separation is paid GPU time)
[if] a streamed WAV, which the shared check exempts [then] the guard passes

-Claude
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import soundfile as sf

import scripts.stem_bundle_worker as worker

pytestmark = pytest.mark.requirement("DECODE-02")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "app-acceptance"


def _locked_fixture() -> tuple[Path, Path]:
    """The truncated mp3 and its uncut carrier, only after the app-acceptance
    loader has checked fixture.json's schema and every recorded sha256
    (``Fixture.load`` raises ``Refused`` on drift), so a changed byte stops the
    test instead of being decoded and adapted to."""
    from scripts.app_acceptance import Fixture

    fixture = Fixture.load(FIXTURES)
    carrier = next(t for t in fixture.tracks if t["file"] == "library/07-acceptance-corrupt-carrier.mp3")
    return fixture.negative_file, FIXTURES / carrier["file"]


TRUNCATED_MP3, CARRIER_MP3 = _locked_fixture()


def _decoded_seconds(path: Path) -> float:
    data, rate = sf.read(str(path))
    return len(data) / rate


def test_the_probe_truncated_mp3_is_refused_before_separation(tmp_path: Path) -> None:
    audio = tmp_path / TRUNCATED_MP3.name
    shutil.copyfile(TRUNCATED_MP3, audio)
    decoded = _decoded_seconds(audio)
    # What the odj-audio path handed _guard_duration as BOTH sides.
    worker._guard_duration(decoded, decoded)

    with pytest.raises(RuntimeError) as raised:
        worker._guard_truncated(audio, decoded)
    message = str(raised.value)
    assert audio.name in message and "truncated" in message, message


def test_control_the_uncut_short_carrier_passes(tmp_path: Path) -> None:
    audio = tmp_path / CARRIER_MP3.name
    shutil.copyfile(CARRIER_MP3, audio)
    worker._guard_truncated(audio, _decoded_seconds(audio))


def test_the_worker_declares_the_tag_reader_its_guard_needs() -> None:
    """The guard reads the claim with tinytag; a PEP 723 env without it would
    raise ImportError on every file, good ones included."""
    source = Path(worker.__file__).read_text(encoding="utf-8")
    block = source.split("# /// script", 1)[1].split("# ///", 1)[0]
    assert '"tinytag' in block, block


def test_a_source_with_no_readable_claim_is_refused(tmp_path: Path) -> None:
    """Fail closed: an .mp3 whose length the tag reader cannot read is not
    stemmed unchecked, even though the decode itself returned audio."""
    audio = tmp_path / "no-claim.mp3"
    audio.write_bytes(b"not an mp3 frame " * 256)
    with pytest.raises(RuntimeError) as raised:
        worker._guard_truncated(audio, 2.0)
    message = str(raised.value)
    assert audio.name in message and "cannot check for truncation" in message, message


def test_control_a_streamed_wav_passes(tmp_path: Path) -> None:
    """Overshoot control: a WAV written to a pipe carries the size sentinel in
    both the RIFF and data headers, the shared check exempts it, and failing
    closed must not turn that exemption into a refusal."""
    # Written here rather than read from a fixture: no canonical WAV is
    # locked by version, manifest and checksum, and an unlocked one could
    # drift with this control still green (Codex P1 on #6816).
    source = tmp_path / "source.wav"
    rate = 44100
    sf.write(str(source), [[0.0, 0.0]] * (rate * 2), rate, subtype="PCM_16")
    assert _decoded_seconds(source) == 2.0, "control: the written WAV is two seconds long"
    audio = tmp_path / "streamed.wav"
    raw = bytearray(source.read_bytes())
    raw[4:8] = b"\xff\xff\xff\xff"
    data_at = raw.index(b"data")
    raw[data_at + 4 : data_at + 8] = b"\xff\xff\xff\xff"
    audio.write_bytes(bytes(raw))
    from apps.shared.audio_playable import _is_streamed_wav

    assert _is_streamed_wav(audio), "the control must be a file the shared check exempts"
    worker._guard_truncated(audio, _decoded_seconds(source))


def test_write_bundle_runs_the_guard_before_separating() -> None:
    """The wiring, not just the decision: in ``write_bundle``'s own source the
    truncation guard runs on the decoded length before the drift guard and
    the first separation.
    Read from the real function, not a stand-in path: torch and demucs never
    enter the repo venv, so the end-to-end run is the test below."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(worker.write_bundle)))
    # ast.walk is breadth-first, so sort by position to get source order.
    named = sorted(
        (n.func for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)),
        key=lambda f: (f.lineno, f.col_offset),
    )
    calls = [func.id for func in named if isinstance(func, ast.Name)]
    order = [n for n in calls if n in {"_load_audio", "_guard_truncated", "_guard_duration", "_separate_all"}]
    # Truncation before the drift guard: with ffmpeg the source duration is
    # the container's claim, so the drift guard would refuse a truncated file
    # first, without naming it as truncated (Codex on #6816).
    assert order[:4] == ["_load_audio", "_guard_truncated", "_guard_duration", "_separate_all"], order


def test_write_bundle_refuses_the_truncated_fixture_end_to_end(tmp_path: Path) -> None:
    """The real worker path, real decoder and real model, on the locked fixture.
    Torch and demucs live only in the worker's PEP 723 environment, never the
    repo venv, so where they are absent this reports UNAVAILABLE rather than
    manufacturing the path."""
    pytest.importorskip("torch", reason="UNAVAILABLE: torch is not in this environment (PEP 723 worker env only)")
    pytest.importorskip("demucs", reason="UNAVAILABLE: demucs is not in this environment (PEP 723 worker env only)")

    audio = tmp_path / TRUNCATED_MP3.name
    shutil.copyfile(TRUNCATED_MP3, audio)
    out_dir = tmp_path / "bundle"
    with pytest.raises(RuntimeError) as raised:
        worker.write_bundle(audio_path=audio, stable_id="sid-truncated", out_dir=out_dir, device="cpu")
    assert audio.name in str(raised.value) and "truncated" in str(raised.value)
    assert not out_dir.exists()
