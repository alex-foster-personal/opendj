"""Build the data manifest for scripts/bench/lyrics_owncrate_listen.html.

Round 3b of the lyrics-align experiment (specs/karaoke-lyrics-alignment.md) took the maintainer's own
27 club tracks through rekordbox pick -> LRCLIB lyrics -> Modal RoFormer vocal stems -> Modal
MMS forced alignment. There is NO ground truth for these tracks, so unlike
lyrics_align_manifest.py this join computes NO error metric: it carries the aligner's own
per-word confidence scores, the round-4a ASR WITNESS verdicts, and the LRCLIB metadata flags,
and nothing that could be mistaken for measured error.

Joins four sources under data/state/lyrics-eval/own-crate/:
  jobs.json          the reference word list, line breaks, LRCLIB metadata, durations
  pred-r3b/<id>.json the aligner output, 1:1 with jobs.json words
  asr-r4a/<id>.json  faster-whisper transcript of the vocal stem (the independent witness)
  audio/<id>.<ext>   the original full mix that the page plays
  stems/<id>-vocals.<ext>  the separated vocal stem the aligner was actually fed, which the
                     page's MIX/VOCALS toggle plays

Usage (repo root):
    uv run --no-sync python scripts/bench/lyrics_owncrate_manifest.py
    uv run --no-sync python scripts/bench/lyrics_owncrate_manifest.py --pred <dir> --out <file>

Fail-fast rules (no silent skips, per repo house rules):
- own-crate dir, jobs.json, pred dir or audio dir missing -> raise.
- a track whose prediction word count or word text disagrees with jobs.json -> raise: this is
  forced alignment, so a mismatch means the two files describe different songs.
- a track with no audio file, no prediction file, or zero/multiple vocal stems -> raise.
  27 tracks in, 27 tracks out.

Requirements
- ✔︎ ✅ Stdlib + repo-internal apps.lyrics only: runs in the repo venv via uv run --no-sync.
    [if] a third-party import creeps in [then ⛔️] move it to PEP 723
- ✔︎ ✅ No error metric of any kind is emitted, derived or implied.
    [if] a field name contains err/aae/acc/gt [then ⛔️] this corpus has no ground truth
- ✔︎ ✅ Every word carries a WITNESS verdict from the round-4a ASR cross-check
  (apps/lyrics/crosscheck.py against asr-r4a/), so the page can gate dot COLOR on an
  independent second model instead of the aligner grading itself. Verdicts:
  agree (matched within WITNESS_AGREE_S) / drift (matched within WITNESS_RED_DELTA_S) /
  contradict (matched beyond it) / lost (unmatched inside a run >= WITNESS_MIN_RUN) /
  unheard (lone unmatched) / unmatchable (token normalizes to nothing).
    [if] a track has no ASR transcript [then ⛔️] raise naming the transcribe-files command,
    never emit words without witness fields
    [if] the witness thresholds here and in the page disagree [then] the page refuses to render
- ✔︎ ✅ The witness is model-vs-model AGREEMENT, never presented as error: verdicts are
  calibrated on JamendoLyrics ground truth (round 5: red = 4.5x base error rate at 73%
  recall, green wrong 0.9% of the time, 0.3 s tolerance) but on this corpus nothing checks
  either model.
    [if] a witness field is named error/accuracy [then ⛔️] two models agreeing can both be wrong
- ✔︎ ✅ Audio URLs are percent-encoded per path part (lesson from the verifier build: 22 of
  79 JamendoLyrics basenames carried non-ASCII and the browser guessed wrong).
    [if] a track id or extension ever carries a space or non-ASCII [then] the URL still resolves
- ✔︎ ✅ duration_delta_s is null-safe and explicit.
    [if] either duration is missing [then] the delta is null, never 0.0
- ✔︎ ✅ Round 4c: the per-track language PROVENANCE rides along, so the page can never
  imply a language was known when it was detected or forced by hand.
    [if] the jobs manifest predates round 4c (no language_source) [then ⛔️] raise, naming
    the regenerate command, rather than inventing a source

-Claude
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from apps.lyrics.crosscheck import (
    WITNESS_AGREE_S,
    WITNESS_LOCAL_WINDOW_S,
    WITNESS_MIN_RUN,
    WITNESS_RED_DELTA_S,
    CrossCheckReport,
    crosscheck,
    witness_verdicts,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
BENCH_DIR: Path = Path(__file__).resolve().parent
OWN_CRATE_DIR: Path = REPO_ROOT / "data" / "state" / "lyrics-eval" / "own-crate"
DEFAULT_JOBS: Path = OWN_CRATE_DIR / "jobs.json"
DEFAULT_PRED_DIR: Path = OWN_CRATE_DIR / "pred-r3b"
DEFAULT_ASR_DIR: Path = OWN_CRATE_DIR / "asr-r4a"
DEFAULT_AUDIO_DIR: Path = OWN_CRATE_DIR / "audio"
DEFAULT_STEM_DIR: Path = OWN_CRATE_DIR / "stems"
DEFAULT_OUT: Path = BENCH_DIR / "lyrics_owncrate_manifest.json"
# serve.py mounts data/state/lyrics-eval/own-crate/ here, so both the mixes and the stems below
# it are reachable through the one mount. --mount swaps the prefix for sibling corpora with the
# same layout (the OLTF run uses /oltf); it is process-global state set once in main().
AUDIO_URL_PREFIX: str = "/owncrate/audio"
STEM_URL_PREFIX: str = "/owncrate/stems"
STEM_EXTENSIONS: tuple[str, ...] = (".mp3", ".flac")
REGENERATE_CMD: str = "uv run --no-sync python scripts/bench/lyrics_owncrate_manifest.py"
TIME_DP: int = 4  # sub-millisecond, keeps the manifest small
SCORE_DP: int = 4
# The two confidence bands the page shades. They are aligner log-probabilities, not errors:
# picked so that the amber band starts where MMS output stops looking like confident speech.
LOW_SCORE: float = -5.0
VERY_LOW_SCORE: float = -8.0
# |lrclib_duration_s - track_duration_s| above this almost always means LRCLIB returned a
# different edit of the song (radio edit vs extended mix), which desynchronises everything.
DURATION_FLAG_S: float = 5.0
# WITNESS verdicts come from apps/lyrics/crosscheck.py's witness_verdicts (the WITNESS_*
# constants above are imported from there, the one canonical home). Round-5 calibration on
# Jamendo ground truth: red = 38.6% P(error) at 72.6% recall, green (agree) = 0.9% P(error),
# false-red 10.8% of correct words.
WITNESS_RED: frozenset[str] = frozenset({"contradict", "lost"})


#-----------------------------------------------------------------------------
# join
#-----------------------------------------------------------------------------


def _read_pred_words(pred_path: Path) -> list[dict[str, Any]]:
    """Parse one prediction file. Raises ValueError with a human-readable contract breach."""
    try:
        payload = json.loads(pred_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{pred_path.name}: unparseable json ({exc})") from exc
    if not isinstance(payload, dict) or "words" not in payload:
        raise ValueError(f"{pred_path.name}: missing top-level 'words' key")
    words = payload["words"]
    if not isinstance(words, list) or not words:
        raise ValueError(f"{pred_path.name}: 'words' is not a non-empty list")
    return words


def _find_audio(audio_dir: Path, name: str) -> Path:
    """The one audio file for this track id. Extension varies, so glob rather than assume mp3."""
    matches = sorted(p for p in audio_dir.glob(f"{name}.*") if p.is_file())
    if not matches:
        raise FileNotFoundError(
            f"no audio for track {name} in {audio_dir} -- the page plays the original full mix, "
            f"so a track without audio cannot be listened to and is not rendered as if it could"
        )
    if len(matches) > 1:
        raise ValueError(
            f"track {name} has {len(matches)} audio files in {audio_dir} "
            f"({[p.name for p in matches]}) -- ambiguous, pick one"
        )
    return matches[0]


def _audio_url(audio_path: Path) -> str:
    """Percent-encode the basename. safe='' so a literal '/' or '#' could not break the mount."""
    return f"{AUDIO_URL_PREFIX}/{quote(audio_path.name, safe='')}"


def _stem_url(stem_dir: Path, name: str) -> str:
    """URL for this track's separated vocal stem. Raises unless EXACTLY one exists.

    That stem is what the aligner was actually fed, so the page's MIX/VOCALS toggle plays the
    aligner's own input. Zero would leave a dead toggle and two would silently pick one of two
    separations, so both are fatal rather than a shrug.
    """
    hits = [p for ext in STEM_EXTENSIONS if (p := stem_dir / f"{name}-vocals{ext}").is_file()]
    if not hits:
        raise FileNotFoundError(
            f"no vocal stem for track {name} in {stem_dir} (looked for "
            f"{name}-vocals{{{','.join(STEM_EXTENSIONS)}}}) -- the MIX/VOCALS toggle needs one "
            f"per track, see specs/karaoke-lyrics-alignment.md"
        )
    if len(hits) > 1:
        raise ValueError(
            f"track {name} has {len(hits)} vocal stems in {stem_dir} ({[p.name for p in hits]}) "
            f"-- ambiguous, two separations of the same track cannot both be 'the' stem"
        )
    return f"{STEM_URL_PREFIX}/{quote(hits[0].name, safe='')}"


def _join_words(job: dict[str, Any], pred_words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Index-align the reference word list to the prediction. Raises on any contract breach."""
    ref_words: list[str] = job["words"]
    line_final: list[bool] = job["line_final"]
    if len(line_final) != len(ref_words):
        raise ValueError(
            f"{job['name']}: jobs.json has {len(ref_words)} words but "
            f"{len(line_final)} line_final flags"
        )
    if len(pred_words) != len(ref_words):
        raise ValueError(
            f"{job['name']}: forced alignment must cover every reference word: "
            f"{len(ref_words)} ref vs {len(pred_words)} pred words"
        )
    joined: list[dict[str, Any]] = []
    for i, (ref, pred) in enumerate(zip(ref_words, pred_words, strict=True)):
        if pred.get("word") != ref:
            raise ValueError(
                f"{job['name']}: word {i} text mismatch: ref {ref!r} vs pred {pred.get('word')!r}"
            )
        start = pred.get("start_s")
        if not isinstance(start, (int, float)) or math.isnan(float(start)):
            raise ValueError(f"{job['name']}: word {i} ({ref!r}) has no usable start_s: {start!r}")
        end = pred.get("end_s")
        score = pred.get("score")
        if not isinstance(score, (int, float)) or math.isnan(float(score)):
            raise ValueError(
                f"{job['name']}: word {i} ({ref!r}) has no usable score: {score!r}. The score IS "
                f"the only honest signal on this corpus, so a missing one is fatal, not skippable"
            )
        joined.append(
            {
                "word": ref,
                "line_final": bool(line_final[i]),
                "start_s": round(float(start), TIME_DP),
                "end_s": None if end is None else round(float(end), TIME_DP),
                "score": round(float(score), SCORE_DP),
            }
        )
    return joined


def _attach_witness(
    name: str, words: list[dict[str, Any]], asr_dir: Path
) -> CrossCheckReport:
    """Stamp apps/lyrics/crosscheck.py's witness verdict onto every word IN PLACE.

    The aligner's own score never influences the verdicts, which is the point -- round 3a showed
    self-confidence is meaningless once the alignment path detaches, so the only trustworthy
    color channel is a second model that never saw the aligner's answer. Round 5 moved the
    verdict derivation into crosscheck.witness_verdicts (local-first matching), so the page, the
    eval CLI and this generator can never disagree.
    """
    asr_path = asr_dir / f"{name}.json"
    if not asr_path.is_file():
        raise FileNotFoundError(
            f"no ASR transcript {asr_path} for track {name} -- the witness verdicts cannot be "
            f"computed without it. Transcribe the stems first:\n"
            f"  uv run --script scripts/modal_asr_spike.py transcribe-files"
        )
    asr_words = json.loads(asr_path.read_text(encoding="utf-8"))["words"]
    lyric_words = [w["word"] for w in words]
    starts = [w["start_s"] for w in words]
    for word, verdict in zip(words, witness_verdicts(lyric_words, starts, asr_words),
                             strict=True):
        word["witness"] = verdict.verdict
        word["asr_delta_s"] = (
            None if verdict.delta_s is None else round(verdict.delta_s, TIME_DP)
        )
    # version_similarity / match_ratio for the track stats still come from the global diff.
    return crosscheck(lyric_words, starts, asr_words)


def _duration_delta_s(job: dict[str, Any]) -> float | None:
    """lrclib_duration_s - track_duration_s, or None when either side is unknown.

    Explicitly null rather than 0.0: "the two durations agree" and "we do not know one of them"
    are different facts and the page renders them differently.
    """
    lrclib = job.get("lrclib_duration_s")
    track = job.get("track_duration_s")
    if lrclib is None or track is None:
        return None
    return round(float(lrclib) - float(track), 3)


def _track_entry(
    job: dict[str, Any], pred_dir: Path, asr_dir: Path, audio_dir: Path, stem_dir: Path
) -> dict[str, Any]:
    name = job["name"]
    pred_path = pred_dir / f"{name}.json"
    if not pred_path.is_file():
        raise FileNotFoundError(
            f"no prediction {pred_path} for track {name} -- re-run the aligner, "
            f"see specs/karaoke-lyrics-alignment.md"
        )
    words = _join_words(job, _read_pred_words(pred_path))
    witness_report = _attach_witness(name, words, asr_dir)
    n_witness_red = sum(1 for w in words if w["witness"] in WITNESS_RED)
    scores = [w["score"] for w in words]
    audio_path = _find_audio(audio_dir, name)
    missing_lang = [k for k in ("language_source", "language_confidence") if k not in job]
    if missing_lang:
        raise ValueError(
            f"{name}: jobs manifest predates round 4c (no {missing_lang}) -- every job must "
            f"say HOW its language was decided, so regenerate with `uv run --script "
            f"scripts/lyrics_owncrate_spike.py make-jobs --force-language 219495710=kik`"
        )
    entry_extra: dict[str, Any] = {}
    if job.get("db_file_path"):
        # Pass-through for corpora matched to state.db by file path (crate):
        # ingest-state --match-by file-path reads this. Absent for oltf/own-crate.
        entry_extra["file_path"] = job["db_file_path"]
    return {
        "name": name,
        "artist": job["artist"],
        "title": job["title"],
        **entry_extra,
        "language_iso": job["language_iso"],
        "language_source": job["language_source"],
        "language_confidence": job["language_confidence"],
        "audio_url": _audio_url(audio_path),
        "stem_audio_url": _stem_url(stem_dir, name),
        "audio_file": audio_path.name,
        "pred_file": pred_path.name,
        "n_words": len(words),
        "stats": {
            "pct_witness_red": round(n_witness_red / len(words), 6),
            "witness_match_ratio": round(witness_report.match_ratio, 4),
            "witness_version_similarity": round(witness_report.version_similarity, 4),
            "mean_score": round(statistics.fmean(scores), SCORE_DP),
            "median_score": round(statistics.median(scores), SCORE_DP),
            "min_score": round(min(scores), SCORE_DP),
            "pct_low_score": round(sum(1 for s in scores if s < LOW_SCORE) / len(scores), 6),
            "pct_very_low_score": round(
                sum(1 for s in scores if s < VERY_LOW_SCORE) / len(scores), 6
            ),
            "duration_delta_s": _duration_delta_s(job),
            # 'lrclib get' era jobs carry lrclib_method; multi-source (OLTF round-2)
            # jobs carry candidate_source + candidate_method. Either present, else raise.
            "source_method": (
                f"{job['candidate_source']} {job['candidate_method']}"
                if "candidate_source" in job
                else f"lrclib {job['lrclib_method']}"
            ),
            "lrclib_duration_s": job.get("lrclib_duration_s"),
            "track_duration_s": job.get("track_duration_s"),
            "first_word_s": words[0]["start_s"],
            "last_word_s": words[-1]["start_s"],
        },
        "words": words,
    }


#-----------------------------------------------------------------------------


def _stamp_file_paths(tracks: list[dict[str, Any]], file_paths: dict[str, str]) -> None:
    """Stamp state.db ``tracks.file_path`` values onto manifest entries IN PLACE.

    For corpora whose jobs carry no ``db_file_path`` (oltf/own-crate are keyed
    by rekordbox vendor id) but where specific tracks must be ingested by file
    path instead (``ingest-state --match-by file-path``): the caller supplies
    an explicit ``{track name -> library file_path}`` map. A name that matches
    no entry raises - a silently dropped stamp would read as 'unmatched at
    ingest' much later, far from the actual mistake.
    """
    by_name = {t["name"]: t for t in tracks}
    unknown = sorted(set(file_paths) - set(by_name))
    if unknown:
        raise ValueError(
            f"--file-paths names not in the jobs manifest: {unknown} -- "
            f"the map must key by job 'name'"
        )
    for name, file_path in file_paths.items():
        by_name[name]["file_path"] = file_path


def build_manifest(
    jobs_path: Path, pred_dir: Path, asr_dir: Path, audio_dir: Path, stem_dir: Path,
    file_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not jobs_path.is_file():
        raise FileNotFoundError(
            f"{jobs_path} missing -- run scripts/lyrics_owncrate_spike.py first, "
            f"see specs/karaoke-lyrics-alignment.md"
        )
    if not pred_dir.is_dir():
        raise FileNotFoundError(f"predictions dir {pred_dir} missing -- run the aligner first")
    if not asr_dir.is_dir():
        raise FileNotFoundError(
            f"ASR transcripts dir {asr_dir} missing -- the witness verdicts need "
            f"`uv run --script scripts/modal_asr_spike.py transcribe-files` first"
        )
    if not audio_dir.is_dir():
        raise FileNotFoundError(f"audio dir {audio_dir} missing -- the page has nothing to play")
    if not stem_dir.is_dir():
        raise FileNotFoundError(
            f"vocal stems dir {stem_dir} missing -- the page's MIX/VOCALS toggle plays what the "
            f"aligner was actually fed, so it cannot be faked from the mix"
        )

    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    if not isinstance(jobs, list) or not jobs:
        raise ValueError(f"{jobs_path} is not a non-empty list of jobs")

    job_names = {job["name"] for job in jobs}
    orphan_preds = sorted({p.stem for p in pred_dir.glob("*.json")} - job_names)
    if orphan_preds:
        raise ValueError(
            f"predictions for tracks not in {jobs_path.name}: {orphan_preds} -- the two are out "
            f"of sync, so the join would silently drop work"
        )

    tracks = [_track_entry(job, pred_dir, asr_dir, audio_dir, stem_dir) for job in jobs]
    if file_paths:
        _stamp_file_paths(tracks, file_paths)
    all_scores = [w["score"] for t in tracks for w in t["words"]]
    n_red = sum(1 for t in tracks for w in t["words"] if w["witness"] in WITNESS_RED)
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "jobs_path": str(jobs_path),
        "pred_dir": str(pred_dir),
        "pred_run": pred_dir.name,
        "asr_dir": str(asr_dir),
        "asr_run": asr_dir.name,
        "audio_dir": str(audio_dir),
        "stem_dir": str(stem_dir),
        "regenerate_command": REGENERATE_CMD,
        "has_ground_truth": False,
        "low_score": LOW_SCORE,
        "very_low_score": VERY_LOW_SCORE,
        "duration_flag_s": DURATION_FLAG_S,
        "witness_agree_s": WITNESS_AGREE_S,
        "witness_red_delta_s": WITNESS_RED_DELTA_S,
        "witness_min_run": WITNESS_MIN_RUN,
        "witness_local_window_s": WITNESS_LOCAL_WINDOW_S,
        "n_tracks": len(tracks),
        "n_words": len(all_scores),
        "pooled_mean_score": round(statistics.fmean(all_scores), SCORE_DP),
        "pooled_pct_low_score": round(
            sum(1 for s in all_scores if s < LOW_SCORE) / len(all_scores), 6
        ),
        "pooled_pct_witness_red": round(n_red / len(all_scores), 6),
        "tracks": tracks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="lyrics_owncrate_manifest.py", description=__doc__)
    parser.add_argument("--jobs", type=Path, default=DEFAULT_JOBS)
    parser.add_argument("--pred", type=Path, default=DEFAULT_PRED_DIR, help="predictions dir")
    parser.add_argument("--asr", type=Path, default=DEFAULT_ASR_DIR, help="ASR transcripts dir")
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO_DIR, help="full-mix audio dir")
    parser.add_argument("--stems", type=Path, default=DEFAULT_STEM_DIR, help="vocal stems dir")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="manifest json to write")
    parser.add_argument("--mount", type=str, default="/owncrate",
                        help="serve.py mount prefix for audio/stem URLs (e.g. /oltf)")
    parser.add_argument("--file-paths", type=Path, default=None,
                        help="JSON map {job name -> state.db tracks.file_path} stamped onto "
                             "entries for ingest-state --match-by file-path (vendor-id-less "
                             "corpora like oltf)")
    args = parser.parse_args()

    global AUDIO_URL_PREFIX, STEM_URL_PREFIX
    mount = "/" + args.mount.strip("/")
    AUDIO_URL_PREFIX = f"{mount}/audio"
    STEM_URL_PREFIX = f"{mount}/stems"

    file_paths: dict[str, str] | None = None
    if args.file_paths is not None:
        file_paths = json.loads(args.file_paths.read_text(encoding="utf-8"))

    manifest = build_manifest(args.jobs, args.pred, args.asr, args.audio, args.stems,
                              file_paths=file_paths)
    args.out.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    size_mb = args.out.stat().st_size / 1e6
    print(
        f"[OK] {args.out} ({size_mb:.2f} MB)\n"
        f"     {manifest['n_tracks']} tracks, {manifest['n_words']} words, run "
        f"{manifest['pred_run']}, witness {manifest['asr_run']}\n"
        f"     pooled mean word score {manifest['pooled_mean_score']:.3f}, "
        f"{manifest['pooled_pct_low_score']:.1%} of words below {LOW_SCORE}\n"
        f"     pooled witness-red {manifest['pooled_pct_witness_red']:.1%} of words "
        f"(matched beyond {WITNESS_RED_DELTA_S}s, or unmatched runs of {WITNESS_MIN_RUN}+)"
    )
    worst = sorted(
        manifest["tracks"], key=lambda t: -t["stats"]["pct_witness_red"]
    )[:3]
    for track in worst:
        print(
            f"     most suspicious: {track['artist']} - {track['title']} "
            f"witness-red {track['stats']['pct_witness_red']:.1%}"
        )
    flagged = [
        t
        for t in manifest["tracks"]
        if t["stats"]["duration_delta_s"] is not None
        and abs(t["stats"]["duration_delta_s"]) > DURATION_FLAG_S
    ]
    print(
        f"     {len(flagged)} track(s) whose LRCLIB duration differs from the file by more than "
        f"{DURATION_FLAG_S}s (likely a different version/edit)"
    )
    for track in flagged:
        print(f"       {track['name']} {track['artist']} - {track['title']}: "
              f"{track['stats']['duration_delta_s']:+.1f}s")
    print("[NOTE] no ground truth exists for this corpus, so this manifest carries NO error metric")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
