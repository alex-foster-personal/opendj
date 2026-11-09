"""Calibration session builder (TRYER-21, TRYER-22): pair fixture, variant selection, taste features, anchor.

- if the committed 24-pair fixture fails its checksum or holds a non-stable id then broken
- if selection repeats a style family while another family exists, or drops a vetoed candidate, then broken
- if a hygiene factor reaches the taste features then broken
- if the anchor is built from decks that do not sum to the mix then broken
"""

from __future__ import annotations

import dataclasses
import json
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile

import scripts.bench.tryer_calibration.build_manifest as builder
from apps.transition_score.contract import Candidate, Measured, TryResult, Veto
from scripts.bench.tryer_calibration.build_manifest import (
    SESSIONS_ROOT,
    AnchorError,
    ClipError,
    _copy_clip,
    anchor_pcm16,
    delayed_anchor,
    layer2_features,
    main,
    read_wav,
    select_taste_variants,
    session_dir_for,
    write_wav_pcm16,
)
from scripts.bench.tryer_calibration.pairs import FIXTURE_PATH, CalibrationPair, PairsError, load_pairs, pairs_checksum


def _candidate(
    cid: str, style: str, score: float | None, out_bar: int = 161, in_bar: int = 17, *, veto: bool = False
) -> Candidate:
    return Candidate(
        id=cid,
        plan_json=json.dumps(
            {"out": {"deck": 1, "window": {"start_bar": out_bar}}, "in": {"deck": 2, "window": {"start_bar": in_bar}}}
        ),
        plan_sha256=cid.encode().hex().ljust(64, "0")[:64],  # a distinct sha that sorts as the id does
        style=style,
        bars=16,
        metrics_json=json.dumps({}),
        factors_json=json.dumps({"alignment": 0.9, "energy_step": 0.3}),
        vetoes=(Veto("clipping", 0.5, -1.0),) if veto else (),
        measured=Measured(5, 7),
        unknown_json=json.dumps({}),
        score=score,
        scorer_version="0.1.0",
        render_path=f"/renders/{cid}.wav",
        render_ms=1.0,
        score_ms=1.0,
    )


@pytest.mark.requirement("TRYER-21")
def test_committed_fixture_is_24_checksummed_stable_id_pairs(tmp_path: Path) -> None:
    """[if] the fixture is loaded [then] 24 pairs verify, and a tampered copy fails loud, [else stop]."""
    pairs = load_pairs()
    assert len(pairs) == 24
    assert len({p.stratum.split("-")[0] for p in pairs}) == 7
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    document["pairs"][0]["in_id"] = document["pairs"][1]["in_id"]
    tampered = tmp_path / "pairs.json"
    tampered.write_text(json.dumps(document), encoding="utf-8", newline="\n")
    with pytest.raises(PairsError, match="sha256"):
        load_pairs(tampered)
    document["pairs"][0]["in_id"] = "not-a-stable-id"
    document["sha256"] = pairs_checksum(document["pairs"])
    tampered.write_text(json.dumps(document), encoding="utf-8", newline="\n")
    with pytest.raises(PairsError, match="40-hex"):
        load_pairs(tampered)


@pytest.mark.requirement("TRYER-22")
def test_taste_v2_crosses_family_and_exit_bar_ignoring_m_and_vetoes() -> None:
    """[if] candidates allow a family x exit factorial [then] (A,X),(B,X),(A,Y) is picked, vetoes kept, [else stop]."""
    # exit bar = out start_bar + 16 bars
    ranked = (
        _candidate("a", "bass_swap_blend", 0.0, out_bar=161, veto=True),
        _candidate("b", "cut", 0.0, out_bar=161),
        _candidate("c", "crossfade", 0.0, out_bar=169, veto=True),
        _candidate("d", "crossfade", 0.0, out_bar=161, veto=True),
        _candidate("e", "long_blend", 0.0, out_bar=177),
    )
    chosen = select_taste_variants(TryResult("o", "i", ranked))
    assert not isinstance(chosen, str)
    assert (chosen.design, [c.id for c in chosen.variants]) == ("factorial", ["a", "d", "e"])
    # M independence: conflicting scores, and the try-er's order (which follows M) reversed, select the same clips.
    from dataclasses import replace

    rescored = tuple(replace(c, score=s) for c, s in zip(reversed(ranked), (0.9, 0.1, 0.7, 0.3, 0.5), strict=True))
    again = select_taste_variants(TryResult("o", "i", rescored))
    assert not isinstance(again, str)
    assert [c.id for c in again.variants] == ["a", "d", "e"]


@pytest.mark.requirement("TRYER-22")
def test_taste_v2_falls_back_with_a_recorded_reason() -> None:
    """[if] no factorial exists [then] the v1 walk is used and the reason names what is missing, [else stop]."""
    own_exits = (
        _candidate("a", "long_blend", 0.0, out_bar=161),
        _candidate("b", "crossfade", 0.0, out_bar=169),
        _candidate("c", "filter_sweep", 0.0, out_bar=177),
    )
    chosen = select_taste_variants(TryResult("o", "i", own_exits))
    assert not isinstance(chosen, str)
    assert chosen.design == "fallback" and chosen.reason == "no exit bar is shared by two families"
    assert [c.id for c in chosen.variants] == ["a", "b", "c"]
    one_family = (_candidate("a", "crossfade", 0.0), _candidate("f", "crossfade", 0.0, out_bar=169))
    chosen = select_taste_variants(TryResult("o", "i", one_family))
    assert not isinstance(chosen, str) and "only one style family" in (chosen.reason or "")
    refused = select_taste_variants(TryResult("o", "i", one_family[:1] * 2))
    assert isinstance(refused, str) and "no taste contrast" in refused


@pytest.mark.requirement("TRYER-22")
def test_taste_features_exclude_hygiene_factors() -> None:
    """[if] a candidate carries hygiene and taste factors [then] only taste ones become features, [else stop]."""
    features = layer2_features(_candidate("a", "long_blend", 0.9))
    assert "alignment" not in features
    assert features["energy_step"] == 0.3
    assert features["family:blend"] == 1.0
    assert features["exit_bar_phrases"] == (161 + 16) / 8
    assert features["out_cue_on_phrase"] == 1.0 and features["in_cue_on_phrase"] == 1.0


@pytest.mark.requirement("TRYER-22")
def test_anchor_delays_incoming_and_refuses_a_deck_sum_that_is_not_the_mix(tmp_path: Path) -> None:
    """[if] decks sum to the mix [then] the anchor is the mix with incoming 40 ms late, else AnchorError, [else stop]."""
    rate = 48000
    rng = np.random.default_rng(0)
    out_deck = rng.uniform(-0.2, 0.2, size=(rate, 2))
    in_deck = rng.uniform(-0.2, 0.2, size=(rate, 2))
    anchor = delayed_anchor(out_deck + in_deck, out_deck, in_deck, rate)
    shift = 1920
    np.testing.assert_allclose(anchor[shift:], out_deck[shift:] + in_deck[:-shift])
    np.testing.assert_allclose(anchor[:shift], out_deck[:shift])
    with pytest.raises(AnchorError, match="differs from the mix"):
        delayed_anchor(out_deck + 0.5 * in_deck, out_deck, in_deck, rate)
    path = tmp_path / "a.wav"
    write_wav_pcm16(path, anchor, rate)
    back, back_rate = read_wav(path)
    assert back_rate == rate and back.shape == anchor.shape
    assert np.max(np.abs(back - anchor)) < 1e-4


@pytest.mark.requirement("TRYER-22")
def test_render_dir_that_contains_the_live_dir_is_refused(tmp_path: Path) -> None:
    """[if] --render-dir is an ancestor of the live data dir [then] the build refuses before deleting anything, [else stop]."""
    live = tmp_path / "home" / "app-data"
    live.mkdir(parents=True)
    keep = live / "state.db"
    keep.write_text("live", encoding="utf-8", newline="\n")
    with pytest.raises(SystemExit, match="contains or is inside"):
        main(["--live-data-dir", str(live), "--render-dir", str(tmp_path / "home"), "--session", "_never"])
    assert keep.read_text(encoding="utf-8") == "live"


def _pcm16_wav(path: Path, samples: np.ndarray, rate: int = 48000) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(samples.shape[1])
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.astype("<i2").tobytes())
    return path


@pytest.mark.requirement("TRYER-22")
def test_clip_is_a_bit_exact_slice_of_the_16_bit_render(tmp_path: Path) -> None:
    """[if] a 16-bit render spanning the whole int16 range is clipped to a window [then] the FLAC decodes to exactly that slice, [else stop]."""
    every_value = np.arange(-32768, 32768, dtype=np.int16)
    render = _pcm16_wav(tmp_path / "mix.wav", np.stack([every_value, every_value[::-1]], axis=1))
    cid = _copy_clip(render, tmp_path, (0.1, 1.2))
    got, _ = soundfile.read(tmp_path / f"{cid}.flac", dtype="int16", always_2d=True)
    np.testing.assert_array_equal(got, np.stack([every_value, every_value[::-1]], axis=1)[4800:57600])


@pytest.mark.requirement("TRYER-22")
def test_float_render_is_refused_not_requantized(tmp_path: Path) -> None:
    """[if] a render is 32-bit float [then] ClipError, never a silently quantized clip, [else stop]."""
    soundfile.write(tmp_path / "mix.wav", np.zeros((4800, 2), dtype=np.float32), 48000, subtype="FLOAT")
    with pytest.raises(ClipError, match="16-bit PCM"):
        _copy_clip(tmp_path / "mix.wav", tmp_path, (0.0, 0.05))


@pytest.mark.requirement("TRYER-22")
def test_anchor_past_full_scale_takes_one_recorded_gain_and_never_clips() -> None:
    """[if] the re-mixed anchor would pass full scale [then] one constant gain is applied and returned, nothing clips, [else stop]."""
    anchor = np.array([[0.5, -0.25], [1.5, -1.2], [0.0, 0.75]])
    samples, gain_db = anchor_pcm16(anchor)
    assert samples.dtype == np.int16 and int(np.abs(samples.astype(int)).max()) == 32767
    gain = 10 ** (gain_db / 20)
    np.testing.assert_allclose(samples / 32768.0, anchor * gain, atol=1 / 32768)
    quiet, quiet_db = anchor_pcm16(anchor / 4)
    assert quiet_db == 0.0 and int(quiet[1, 0]) == round(1.5 / 4 * 32768)


@pytest.mark.requirement("TRYER-22")
@pytest.mark.parametrize("name", ["../x", "a/b", "/tmp/x", "..", ".hidden", "a..b"])
def test_session_name_stays_beneath_the_sessions_root(name: str) -> None:
    """[if] --session carries a separator, an absolute path or '..' [then] SystemExit before any write, [else stop]."""
    with pytest.raises(SystemExit, match=r"plain name|resolves outside"):
        session_dir_for(name)
    assert session_dir_for("oltf-r0").parent == SESSIONS_ROOT.resolve()


@pytest.mark.requirement("TRYER-22")
def test_existing_session_is_never_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] --session names a session that already exists [then] SystemExit and its key.json is untouched, [else stop]."""
    monkeypatch.setattr(builder, "SESSIONS_ROOT", tmp_path / "sessions")
    existing = tmp_path / "sessions" / "taken"
    existing.mkdir(parents=True)
    (existing / "key.json").write_text("original", encoding="utf-8", newline="\n")
    live = tmp_path / "live"
    live.mkdir()
    with pytest.raises(SystemExit, match="already exists"):
        main(["--live-data-dir", str(live), "--render-dir", str(tmp_path / "renders"), "--session", "taken"])
    assert (existing / "key.json").read_text(encoding="utf-8") == "original"


@pytest.mark.requirement("TRYER-22")
@pytest.mark.parametrize(("fold", "bars_kept"), [(1.0, 2.0), (0.5, 2.0), (2.0, 2.0)])
def test_trailing_trim_keeps_two_plan_bars_at_any_tempo_fold(fold: float, bars_kept: float) -> None:
    """[if] the incoming deck is matched at half, normal or double time [then] the trim drops exactly 2 plan-clock bars, [else stop]."""
    in_bpm, ratio = 124.0, 1.02
    plan_bar_s = fold * 240.0 / in_bpm / ratio
    trimmed = builder.trailing_trim_s(in_bpm, ratio, fold)
    assert trimmed == pytest.approx((builder.CFG.CONTEXT_BARS - bars_kept) * plan_bar_s)
    assert trimmed != pytest.approx(2 * 240.0 / in_bpm / ratio) or fold == 1.0


@pytest.mark.requirement("TRYER-22")
def test_invalid_session_name_refused_before_any_directory_is_made(tmp_path: Path) -> None:
    """[if] --session is invalid [then] SystemExit and the render dir is never created, [else stop]."""
    live = tmp_path / "live"
    live.mkdir()
    render = tmp_path / "renders-not-yet"
    with pytest.raises(SystemExit, match="plain name"):
        main(["--live-data-dir", str(live), "--render-dir", str(render), "--session", "../escape"])
    assert not render.exists()


@pytest.mark.requirement("TRYER-22")
def test_build_pair_keeps_renders_in_its_own_render_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the bench tries a pair [then] it asks try_pair to keep renders under its own render dir, [else stop]."""
    from scripts.bench.tryer_calibration import build_manifest

    seen: dict[str, Any] = {}

    class Stop(Exception):
        pass

    def fake_try_pair(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise Stop

    monkeypatch.setattr(build_manifest, "try_pair", fake_try_pair)
    pair = CalibrationPair("p1", "stratum", "out", "in")
    with pytest.raises(Stop):
        build_manifest.build_pair(pair, object(), tmp_path, tmp_path / "clips", 2)  # type: ignore[arg-type]
    assert seen["keep_renders_in"] == tmp_path / build_manifest.KEPT_RENDERS


@pytest.mark.requirement("TRYER-22")
def test_a_candidate_with_no_kept_render_is_an_anchor_error() -> None:
    """[if] a candidate has no kept render (refused, or nothing kept) [then] AnchorError names it, [else stop]."""
    from scripts.bench.tryer_calibration import build_manifest

    candidate = dataclasses.replace(_candidate("c1", "blend", 0.5), render_path=None)
    with pytest.raises(build_manifest.AnchorError, match="c1"):
        build_manifest.kept_render(candidate)


@pytest.mark.requirement("TRYER-22")
def test_the_manifest_records_the_checksum_the_pairs_were_verified_against(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the fixture changes on disk after the pairs were loaded [then] the manifest records the loaded checksum, [else stop]."""
    import argparse

    from scripts.bench.tryer_calibration.pairs import load_pairs_verified

    fixture = tmp_path / "pairs.json"
    fixture.write_text(FIXTURE_PATH.read_text(encoding="utf-8"))
    _, loaded = load_pairs_verified(fixture)
    fixture.write_text(fixture.read_text(encoding="utf-8").replace(loaded, "f" * 64))  # restamped mid-build
    session = tmp_path / "session"
    session.mkdir()
    args = argparse.Namespace(session="s", pairs=fixture)
    monkeypatch.setattr(builder, "BENCH_ROOT", tmp_path)
    builder._write_session(session, args, [], {}, {}, loaded)
    assert json.loads((session / "manifest.json").read_text(encoding="utf-8"))["pairs_fixture_sha256"] == loaded


def test_session_artifacts_are_written_with_lf_utf8_so_the_key_hash_matches_the_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the session is written on any platform [then] key.manifest_sha256 is the sha of the manifest bytes on disk, [else stop]."""
    import argparse
    import hashlib

    session = tmp_path / "session"
    session.mkdir()
    monkeypatch.setattr(builder, "BENCH_ROOT", tmp_path)
    builder._write_session(
        session, argparse.Namespace(session="s", pairs=tmp_path), [], {}, {"c": {"note": "café"}}, "a" * 64
    )
    raw = (session / "manifest.json").read_bytes()
    assert b"\r\n" not in raw and b"\r\n" not in (session / "key.json").read_bytes()
    assert (
        json.loads((session / "key.json").read_text(encoding="utf-8"))["manifest_sha256"]
        == hashlib.sha256(raw).hexdigest()
    )


def test_refused_candidates_are_never_selected_for_rating() -> None:
    """[if] try_pair refuses a candidate [then] taste selection never picks it, [else stop]."""
    from dataclasses import replace

    ranked = (
        _candidate("a", "bass_swap_blend", 0.0, out_bar=161),
        _candidate("b", "cut", 0.0, out_bar=161),
        _candidate("d", "crossfade", 0.0, out_bar=161),
        _candidate("e", "long_blend", 0.0, out_bar=177),
    )
    control = select_taste_variants(TryResult("o", "i", ranked))
    assert not isinstance(control, str) and "a" in [c.id for c in control.variants]
    refused_a = (replace(ranked[0], refused="render failed", render_path=None), *ranked[1:])
    chosen = select_taste_variants(TryResult("o", "i", refused_a))
    assert not isinstance(chosen, str) and "a" not in [c.id for c in chosen.variants]
    all_refused = tuple(replace(c, refused="render failed", render_path=None) for c in ranked)
    none_left = select_taste_variants(TryResult("o", "i", all_refused))
    assert isinstance(none_left, str) and "no rendered candidates" in none_left


@pytest.mark.requirement("TRYER-22")
def test_a_failed_build_leaves_no_session_and_a_good_one_lands_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a pair fails fatally mid-build [then] no session or staging folder is left and a retry lands whole, [else stop]."""
    import argparse

    from scripts.bench.tryer_calibration import build_manifest as bm

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    pairs = [argparse.Namespace(pair_id="p1"), argparse.Namespace(pair_id="p2")]
    monkeypatch.setattr(bm, "BENCH_ROOT", tmp_path)
    monkeypatch.setattr(bm, "_validated_paths", lambda args: (sessions / args.session, tmp_path, tmp_path / "r"))
    monkeypatch.setattr(bm, "_selected_pairs", lambda args: (pairs, "a" * 64))
    monkeypatch.setattr(bm, "ReadOnlyLiveSource", lambda live, profiles: None)
    monkeypatch.setattr(bm, "_require_disk", lambda *a: None)
    fail_on: set[str] = {"p2"}

    def build_one(pair: argparse.Namespace, source: object, render_dir: Path, clips: Path) -> tuple[list[str], dict]:
        if pair.pair_id in fail_on:
            raise RuntimeError("missing audio")
        (clips / f"{pair.pair_id}.flac").write_bytes(b"x")
        return [pair.pair_id], {pair.pair_id: {"pair_id": pair.pair_id}}

    monkeypatch.setattr(bm, "_build_one", build_one)
    argv = ["--live-data-dir", str(tmp_path), "--render-dir", str(tmp_path / "r"), "--session", "s1"]
    with pytest.raises(RuntimeError, match="missing audio"):
        bm.main(argv)
    assert list(sessions.iterdir()) == []  # no partial session, no staging folder
    fail_on.clear()
    assert bm.main(argv) == 0  # control: the same name retries and lands whole
    assert sorted(p.name for p in sessions.iterdir()) == ["s1"]
    assert {"manifest.json", "key.json", "clips"} <= {p.name for p in (sessions / "s1").iterdir()}


@pytest.mark.requirement("TRYER-22")
@pytest.mark.parametrize(
    ("raised", "skipped"),
    [
        ("pair", True),
        ("missing-audio", True),
        ("dj-not-found", True),
        ("renderer", False),
        ("dj-engine", False),
        ("clip", False),
    ],
)
def test_only_pair_level_failures_skip_a_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raised: str, skipped: bool
) -> None:
    """[if] only this pair cannot be tried [then] it is skipped, while a renderer or engine failure aborts, [else stop]."""
    import argparse

    from apps.opendj_cli.dj_service import DjError
    from apps.transition_score.tryer import AudioUnavailable, PairNotTryable, TryError
    from scripts.bench.tryer_calibration import build_manifest as bm

    errors = {
        "pair": PairNotTryable("no valid transition proposed"),
        "missing-audio": AudioUnavailable("trk-in"),
        "dj-not-found": DjError("not_found", "GET /tracks/x -> 404"),
        "renderer": TryError("odj-audio could not start"),
        "dj-engine": DjError("engine_not_running", "no engine"),
        "clip": ClipError("candidate c1 missing deck2.wav"),
    }

    def failing_build_pair(*_args: object, **_kwargs: object) -> None:
        raise errors[raised]

    monkeypatch.setattr(bm, "build_pair", failing_build_pair)
    pair = argparse.Namespace(pair_id="p1")
    if skipped:
        assert isinstance(bm._build_one(pair, None, tmp_path, tmp_path), str)  # type: ignore[arg-type]
    else:
        with pytest.raises(type(errors[raised])):
            bm._build_one(pair, None, tmp_path, tmp_path)  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []  # the pair's render child is removed either way


def test_the_live_source_turns_missing_audio_into_a_pair_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the resolver finds no audio file [then] the live source raises AudioUnavailable, a pair skip, [else stop]."""
    from apps.transition_score.tryer import AudioUnavailable
    from scripts.bench.tryer_calibration import build_manifest as bm

    def no_audio_file(data_dir: Path, stable_id: str) -> Path:
        raise FileNotFoundError(f"/srv/music/{stable_id}.mp3")

    monkeypatch.setattr("apps.stems.cli.resolve_audio_path", no_audio_file)
    source = bm.ReadOnlyLiveSource(Path("/srv/live"), Path("/srv/profiles"))
    with pytest.raises(AudioUnavailable) as gone:
        source.audio_path("trk-in")
    assert isinstance(gone.value, bm.SKIPPABLE_PAIR_ERRORS) and "/srv" not in str(gone.value)


@pytest.mark.requirement("TRYER-22")
def test_deck_slices_are_bit_exact_and_the_anchor_delays_only_its_incoming_deck(tmp_path: Path) -> None:
    """[if] decks are copied beside a clip [then] each is its exact int16 slice, the anchor's incoming 40 ms late, [else stop].

    The slice covers the played window of the kept deck render; the anchor's outgoing deck is untouched."""
    rate = 48000
    render = tmp_path / "kept" / "c1"
    (render / "decks").mkdir(parents=True)
    rng = np.random.default_rng(7)
    decks = {n: (rng.integers(-20000, 20000, size=(rate * 4, 2))).astype(np.int16) for n in (1, 2)}
    for n, samples in decks.items():
        soundfile.write(str(render / "decks" / f"deck{n}.wav"), samples, rate, subtype="PCM_16")
    candidate = dataclasses.replace(_candidate("c1", "blend", 0.5), render_path=str(render / "mix.wav"))
    (render / "mix.wav").write_bytes(b"")  # kept_render only checks the path is recorded
    clips = tmp_path / "clips"
    window = (1.0, 3.0)
    paths = builder._copy_decks(candidate, clips, "aa", window)
    assert paths == {"1": "clips/aa/decks/deck1.flac", "2": "clips/aa/decks/deck2.flac"}
    for n in (1, 2):
        sliced, _ = soundfile.read(str(clips / "aa" / "decks" / f"deck{n}.flac"), dtype="int16")
        assert np.array_equal(sliced, decks[n][rate * 1 : rate * 3]), "if a deck slice is not bit-exact then lanes lie"
    builder._copy_decks(candidate, clips, "bb", window, in_delay_ms=40.0)
    late, _ = soundfile.read(str(clips / "bb" / "decks" / "deck2.flac"), dtype="int16")
    shift = round(0.040 * rate)
    assert np.array_equal(late, decks[2][rate - shift : rate * 3 - shift]), "if the anchor's incoming is not 40 ms late"
    same, _ = soundfile.read(str(clips / "bb" / "decks" / "deck1.flac"), dtype="int16")
    assert np.array_equal(same, decks[1][rate : rate * 3]), (
        "if the anchor moves the outgoing deck then it is not taste1"
    )


def _kept_candidate(tmp_path: Path, cid: str = "c1") -> tuple[Candidate, Path]:
    """A candidate whose mix sits at ``tmp_path/kept/<cid>/mix.wav`` (decks sibling optional)."""
    render = tmp_path / "kept" / cid
    render.mkdir(parents=True)
    (render / "mix.wav").write_bytes(b"")
    candidate = dataclasses.replace(_candidate(cid, "blend", 0.5), render_path=str(render / "mix.wav"))
    return candidate, render


def _write_pcm16_deck(render: Path, n: int, rate: int = 48000) -> None:
    decks = render / "decks"
    decks.mkdir(parents=True, exist_ok=True)
    samples = np.zeros((rate, 2), dtype=np.int16)
    soundfile.write(str(decks / f"deck{n}.wav"), samples, rate, subtype="PCM_16")


@pytest.mark.requirement("TRYER-22")
def test_copy_decks_raises_when_only_the_outgoing_deck_wav_is_kept(tmp_path: Path) -> None:
    """[if] a clip's incoming deck render is missing [then] ClipError names the candidate and file, [else stop]."""
    candidate, render = _kept_candidate(tmp_path)
    _write_pcm16_deck(render, 1)
    clips = tmp_path / "clips"
    with pytest.raises(ClipError, match=r"candidate c1 missing deck2\.wav"):
        builder._copy_decks(candidate, clips, "aa", (1.0, 3.0))
    assert not (clips / "aa" / "decks").exists()
    assert ClipError not in builder.SKIPPABLE_PAIR_ERRORS


@pytest.mark.requirement("TRYER-22")
def test_copy_decks_raises_when_the_decks_directory_is_missing(tmp_path: Path) -> None:
    """[if] a clip's decks directory is missing [then] ClipError names the candidate and first missing file, [else stop]."""
    candidate, _render = _kept_candidate(tmp_path)
    clips = tmp_path / "clips"
    with pytest.raises(ClipError, match=r"candidate c1 missing deck1\.wav"):
        builder._copy_decks(candidate, clips, "aa", (1.0, 3.0))
    assert not (clips / "aa" / "decks").exists()


@pytest.mark.requirement("AGENT-39")
def test_a_lull_then_drop_candidate_is_added_beside_the_taste_variants_never_into_them() -> None:
    """[if] a pair renders lull_then_drop candidates [then] the first by plan_sha256 is added beside the factorial, [else stop].

    It never enters the taste factorial, and with none rendered nothing is added."""
    from scripts.bench.tryer_calibration.build_manifest import lull_candidate

    ranked = (
        _candidate("c1", "bass_swap_blend", 0.6, 161),
        _candidate("c2", "crossfade", 0.5, 161),
        _candidate("c3", "bass_swap_blend", 0.4, 169),
        _candidate("c5", "lull_then_drop", 0.2, 145),
        _candidate("c4", "lull_then_drop", 0.3, 145),
    )
    selection = select_taste_variants(TryResult("o", "i", ranked))
    assert not isinstance(selection, str)
    assert all(c.style != "lull_then_drop" for c in selection.variants), (
        "if a lull enters the factorial then D13 leaked"
    )
    lull = lull_candidate(TryResult("o", "i", ranked), selection.variants)
    assert lull is not None and lull.id == "c4"
    assert lull_candidate(TryResult("o", "i", ranked[:3]), selection.variants) is None


@pytest.mark.requirement("AGENT-39")
def test_lull_then_drop_stays_out_of_taste_when_only_one_core_lane_family() -> None:
    """[if] only one core-lane family is rendered plus a lull [then] the lull stays out of taste, [else stop].

    Crossfades at two exits plus a lull at the first exit cannot factorial in CORE_LANES; the any-family
    retry would otherwise take the lull as family B."""
    from scripts.bench.tryer_calibration.build_manifest import lull_candidate

    ranked = (
        _candidate("cx", "crossfade", 0.5, out_bar=161),
        _candidate("cy", "crossfade", 0.4, out_bar=169),
        _candidate("lx", "lull_then_drop", 0.3, out_bar=161),
    )
    selection = select_taste_variants(TryResult("o", "i", ranked))
    assert not isinstance(selection, str)
    assert all(c.style != "lull_then_drop" for c in selection.variants)
    assert all(c.style == "crossfade" for c in selection.variants)
    lull = lull_candidate(TryResult("o", "i", ranked), selection.variants)
    assert lull is not None and lull.id == "lx"


@pytest.mark.requirement("AGENT-39")
def test_only_lull_then_drop_candidates_return_no_taste_contrast() -> None:
    """[if] every rendered candidate is lull_then_drop [then] select_taste_variants returns no-contrast, [else stop].

    Two lulls at different exits would otherwise become a 2-clip v1-walk Selection."""
    lulls = (
        _candidate("l1", "lull_then_drop", 0.3, out_bar=161),
        _candidate("l2", "lull_then_drop", 0.2, out_bar=169),
    )
    got = select_taste_variants(TryResult("o", "i", lulls))
    assert isinstance(got, str) and "no taste contrast" in got
