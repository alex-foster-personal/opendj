"""Build the data manifest for scripts/bench/lyrics_align_verifier.html.

Joins JamendoLyrics ground truth (apps.lyrics.jamendo) to a predictions dir written by an
aligner run (contract in specs/karaoke-lyrics-alignment.md) and emits ONE JSON the karaoke
verification page fetches. Metrics come from apps.lyrics.metrics, never recomputed here, so
the page and `python -m apps.lyrics score` can never disagree.

Usage (repo root):
    uv run --no-sync python scripts/bench/lyrics_align_manifest.py
    uv run --no-sync python scripts/bench/lyrics_align_manifest.py --pred <dir> --out <file>

Re-run it while an aligner is still writing predictions: songs without a prediction file are
reported as unavailable, not faked, and the next run picks them up.

Beyond the join, the derived signals here are the displacement marks. `displaced_to_repeat`: a
badly placed word whose predicted onset lands on ANOTHER ground-truth occurrence of the same
word. In a repeated chorus that is the aligner locking onto the wrong repeat rather than
guessing at random, which is a different failure with a different fix, so the page marks it
differently. `displaced_by_phrase` extends that to whole phrases: a word with no repeat of its
own text still moved WITH the repeat when it sits in a contiguous misplaced run that shifted
together and whose majority is per-word displaced (details on _mark_displaced_phrases).

Every song also carries `stem_audio_url`, the separated vocal stem that serve.py mounts at
/jamendo-vocals. That is what the round-1 and round-2 aligners were actually fed, so the page's
MIX/VOCALS toggle can play the aligner's own input rather than only the mix a DJ hears. The
round-0 aligner heard the full mix, so on that manifest VOCALS is a listening aid, not the
aligner's input.

Fail-fast rules (no silent skips, per repo house rules):
- predictions dir missing, or empty, or holding a json for a song not in the dataset -> raise.
- vocal stems dir missing, or a song with zero or more than one `<name>-vocals.{mp3,flac}` ->
  raise. A missing stem would leave a dead toggle; two would silently pick one separation.
- a song whose prediction breaks the forced-alignment contract (word count or word text
  mismatch, unparseable json, NaN onset) is emitted WITH an `error` string and no words, so
  the page renders an explicit error card for it.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from apps.lyrics.jamendo import (
    DEFAULT_DATASET_DIR,
    EXPECTED_SONGS,
    JamendoSong,
    load_songs,
)
from apps.lyrics.metrics import OnsetErrorReport, aggregate, score_onsets

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
BENCH_DIR: Path = Path(__file__).resolve().parent
DEFAULT_PRED_DIR: Path = REPO_ROOT / "data" / "state" / "lyrics-eval" / "round0-mms-fullmix"
DEFAULT_OUT: Path = BENCH_DIR / "lyrics_align_manifest.json"
# serve.py mounts the jamendolyrics dataset here (symlinks followed).
AUDIO_URL_PREFIX: str = "/jamendo/mp3"
# ... and the separated vocal stems here. These are what the round-1 and round-2 aligners were
# actually fed, so the page can play the aligner's own input rather than only the mix a DJ hears.
DEFAULT_STEM_DIR: Path = REPO_ROOT / "data" / "datasets" / "jamendolyrics-vocals"
STEM_URL_PREFIX: str = "/jamendo-vocals"
STEM_EXTENSIONS: tuple[str, ...] = (".mp3", ".flac")
REGENERATE_CMD: str = "uv run --no-sync python scripts/bench/lyrics_align_manifest.py"
TIME_DP: int = 4  # sub-millisecond, keeps the manifest a third of the size of full floats
# Same 300 ms the scorer reports acc@300ms at, used twice: a word is only a displacement
# CANDIDATE above it, and only counts as landing on a repeat within it.
DISPLACE_TOL_S: float = 0.3
# Phrase grouping: a misplaced run counts as "moved together" when every word's signed offset
# sits within this band of the run's median offset, and only runs of at least
# PHRASE_MIN_RUN words with a strict per-word-displaced majority earn the mark.
PHRASE_OFFSET_TOL_S: float = 0.3
PHRASE_MIN_RUN: int = 3
_EDGE_PUNCT = re.compile(r"^\W+|\W+$", re.UNICODE)


#-----------------------------------------------------------------------------
# ground truth + prediction join
#-----------------------------------------------------------------------------


def _metrics_dict(report: OnsetErrorReport) -> dict[str, Any]:
    return {
        "n_words": report.n_words,
        "aae_s": round(report.mean_abs_error_s, 6),
        "median_s": round(report.median_abs_error_s, 6),
        "p95_s": round(report.p95_abs_error_s, 6),
        "acc": {f"{int(tol * 1000)}ms": round(frac, 6) for tol, frac in report.within.items()},
    }


def _read_pred_words(pred_path: Path) -> list[dict[str, Any]]:
    """Parse one prediction file. Raises ValueError with a human-readable contract breach."""
    try:
        payload = json.loads(pred_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"unparseable json ({exc}) -- a partially written file re-reads fine")
    if not isinstance(payload, dict) or "words" not in payload:
        raise ValueError("missing top-level 'words' key")
    words = payload["words"]
    if not isinstance(words, list) or not words:
        raise ValueError("'words' is not a non-empty list")
    return words


def _normalize_word(word: str) -> str:
    """Casefold and strip surrounding punctuation, so 'Fire,' and 'fire' are one token."""
    return _EDGE_PUNCT.sub("", word).casefold()


def _mark_displaced_to_repeat(joined: list[dict[str, Any]]) -> None:
    """Flag words that are not merely late, but attached to ANOTHER occurrence of themselves.

    Once an aligner loses lock in a repeated chorus, its onsets keep landing on real singing of
    the same word, just the wrong repeat. That reads as a catastrophic error in the metrics and
    as a plausible-looking highlight on screen, so it earns its own mark rather than sharing the
    red bucket with a word placed over silence. Mutates `joined` in place, adding the key only
    where true (absent means false, which keeps the manifest small).
    """
    norms = [_normalize_word(w["word"]) for w in joined]
    # norm -> [(index, gt onset)], so the j != i rule is checked on the index, not on a float.
    occurrences: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for j, norm in enumerate(norms):
        if norm:  # a bare punctuation token has no identity to repeat
            occurrences[norm].append((j, joined[j]["gt_start_s"]))
    for i, word in enumerate(joined):
        if word["abs_err_s"] <= DISPLACE_TOL_S or not norms[i]:
            continue
        for j, gt_start in occurrences[norms[i]]:
            if j == i:
                continue
            if abs(word["pred_start_s"] - gt_start) <= DISPLACE_TOL_S:
                word["displaced_to_repeat"] = True
                word["repeat_gt_start_s"] = round(gt_start, TIME_DP)
                break


def _coherent_pieces(offsets: list[float], start: int, end: int) -> list[tuple[int, int]]:
    """Split [start, end) into pieces whose offsets all sit within PHRASE_OFFSET_TOL_S of the
    piece median, splitting at the largest consecutive-offset jump until each piece coheres."""
    piece = offsets[start:end]
    med = sorted(piece)[len(piece) // 2]
    if all(abs(o - med) <= PHRASE_OFFSET_TOL_S for o in piece):
        return [(start, end)]
    if end - start < 2:
        raise AssertionError("a single offset always coheres with its own median")
    split = max(range(start + 1, end), key=lambda k: abs(offsets[k] - offsets[k - 1]))
    return _coherent_pieces(offsets, start, split) + _coherent_pieces(offsets, split, end)


def _mark_displaced_phrases(joined: list[dict[str, Any]]) -> None:
    """Extend the per-word displaced mark to whole phrases that moved together.

    A word that is not itself within DISPLACE_TOL_S of another occurrence of its own text (so
    _mark_displaced_to_repeat cannot flag it) still moved WITH the repeat when it sits inside a
    contiguous run of misplaced words (all >DISPLACE_TOL_S error) that (a) shifted together --
    every signed offset within PHRASE_OFFSET_TOL_S of the run's median offset, splitting the run
    at its largest offset discontinuities until each piece coheres -- and (b) is at least
    PHRASE_MIN_RUN words with a strict majority already carrying displaced_to_repeat. Those
    extras get `displaced_by_phrase` (a separate flag: the per-word count stays honest) plus the
    run's shared shift as `phrase_offset_s`. Must run AFTER _mark_displaced_to_repeat.
    """
    offsets = [w["pred_start_s"] - w["gt_start_s"] for w in joined]
    run_start: int | None = None
    for i in range(len(joined) + 1):
        bad = i < len(joined) and joined[i]["abs_err_s"] > DISPLACE_TOL_S
        if bad and run_start is None:
            run_start = i
        elif not bad and run_start is not None:
            for lo, hi in _coherent_pieces(offsets, run_start, i):
                if hi - lo < PHRASE_MIN_RUN:
                    continue
                n_displaced = sum(
                    1 for w in joined[lo:hi] if w.get("displaced_to_repeat") is True
                )
                if n_displaced * 2 <= hi - lo:
                    continue
                shift = sorted(offsets[lo:hi])[(hi - lo) // 2]
                for w in joined[lo:hi]:
                    if w.get("displaced_to_repeat") is not True:
                        w["displaced_by_phrase"] = True
                        w["phrase_offset_s"] = round(shift, TIME_DP)
            run_start = None


def _join_words(song: JamendoSong, pred_words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Index-align ground truth to prediction. Raises on any forced-alignment contract breach."""
    if len(pred_words) != len(song.words):
        raise ValueError(
            f"forced alignment must cover every reference word: "
            f"{len(song.words)} ref vs {len(pred_words)} pred words"
        )
    joined: list[dict[str, Any]] = []
    for i, (ref, pred) in enumerate(zip(song.words, pred_words, strict=True)):
        if pred.get("word") != ref.word:
            raise ValueError(
                f"word {i} text mismatch: ref {ref.word!r} vs pred {pred.get('word')!r}"
            )
        pred_start = pred.get("start_s")
        if not isinstance(pred_start, (int, float)) or math.isnan(float(pred_start)):
            raise ValueError(f"word {i} ({ref.word!r}) has no usable start_s: {pred_start!r}")
        pred_end = pred.get("end_s")
        score = pred.get("score")
        joined.append(
            {
                "word": ref.word,
                "line_final": ref.line_final,
                "gt_start_s": round(ref.start_s, TIME_DP),
                "gt_end_s": None if ref.end_s is None else round(ref.end_s, TIME_DP),
                "pred_start_s": round(float(pred_start), TIME_DP),
                "pred_end_s": None if pred_end is None else round(float(pred_end), TIME_DP),
                "score": None if score is None else round(float(score), 4),
                "abs_err_s": round(abs(ref.start_s - float(pred_start)), TIME_DP),
            }
        )
    _mark_displaced_to_repeat(joined)
    _mark_displaced_phrases(joined)
    return joined


def _stem_url(stem_dir: Path, name: str) -> str:
    """URL for this song's separated vocal stem. Raises unless EXACTLY one exists.

    The set is mixed: 43 songs come back as mp3 and 36 as flac from the separation run. Two files
    for one song would mean the page silently picks one of two different separations, and zero
    means the toggle would be dead, so both are fatal rather than a shrug.
    """
    hits = [p for ext in STEM_EXTENSIONS if (p := stem_dir / f"{name}-vocals{ext}").is_file()]
    if not hits:
        raise FileNotFoundError(
            f"no vocal stem for {name} in {stem_dir} (looked for "
            f"{name}-vocals{{{','.join(STEM_EXTENSIONS)}}}) -- the MIX/VOCALS toggle needs one "
            f"per song, see specs/karaoke-lyrics-alignment.md"
        )
    if len(hits) > 1:
        raise ValueError(
            f"{name} has {len(hits)} vocal stems in {stem_dir} ({[p.name for p in hits]}) -- "
            f"ambiguous, two separations of the same song cannot both be 'the' stem"
        )
    return f"{STEM_URL_PREFIX}/{quote(hits[0].name, safe='')}"


def _song_entry(song: JamendoSong, pred_path: Path | None, stem_dir: Path) -> dict[str, Any]:
    """One manifest row. `error` non-null means the page must render an error card, not a player."""
    entry: dict[str, Any] = {
        "name": song.name,
        "artist": song.artist,
        "title": song.title,
        "language": song.language,
        # 22 of the 79 basenames carry non-ASCII (Fussabdruecke, Haeblame, ...) or '!', so the
        # URL is percent-encoded here rather than relying on the browser to guess.
        "audio_url": f"{AUDIO_URL_PREFIX}/{quote(song.mp3_path.name, safe='')}",
        "stem_audio_url": _stem_url(stem_dir, song.name),
        "flags": {
            "lyric_overlap": song.lyric_overlap,
            "polyphonic": song.polyphonic,
            "non_lexical": song.non_lexical,
        },
        "n_ref_words": len(song.words),
        "has_prediction": pred_path is not None,
        "pred_file": None if pred_path is None else pred_path.name,
        "error": None,
        "metrics": None,
        "n_displaced_to_repeat": 0,
        "n_displaced_by_phrase": 0,
        "words": [],
    }
    if pred_path is None:
        return entry
    try:
        words = _join_words(song, _read_pred_words(pred_path))
    except ValueError as exc:
        entry["error"] = str(exc)
        return entry
    entry["words"] = words
    entry["n_displaced_to_repeat"] = sum(1 for w in words if w.get("displaced_to_repeat"))
    entry["n_displaced_by_phrase"] = sum(1 for w in words if w.get("displaced_by_phrase"))
    entry["metrics"] = _metrics_dict(
        score_onsets([w["gt_start_s"] for w in words], [w["pred_start_s"] for w in words])
    )
    return entry


#-----------------------------------------------------------------------------


def build_manifest(dataset_dir: Path, pred_dir: Path, stem_dir: Path) -> dict[str, Any]:
    if not pred_dir.is_dir():
        raise FileNotFoundError(
            f"predictions dir {pred_dir} missing -- run the aligner first, "
            f"see specs/karaoke-lyrics-alignment.md"
        )
    if not stem_dir.is_dir():
        raise FileNotFoundError(
            f"vocal stems dir {stem_dir} missing -- the page's MIX/VOCALS toggle plays what the "
            f"round-1 and round-2 aligners were actually fed, so it cannot be faked from the mix"
        )
    songs = load_songs(dataset_dir)
    pred_files = {p.stem: p for p in sorted(pred_dir.glob("*.json"))}
    if not pred_files:
        raise ValueError(f"no *.json predictions in {pred_dir} -- nothing to verify yet")
    unknown = sorted(set(pred_files) - {s.name for s in songs})
    if unknown:
        raise ValueError(f"predictions for songs not in the dataset: {unknown}")

    entries = [_song_entry(song, pred_files.get(song.name), stem_dir) for song in songs]
    scored = [e for e in entries if e["metrics"] is not None]
    pooled = (
        _metrics_dict(
            aggregate(
                [
                    score_onsets(
                        [w["gt_start_s"] for w in e["words"]],
                        [w["pred_start_s"] for w in e["words"]],
                    )
                    for e in scored
                ]
            )
        )
        if scored
        else None
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset_dir": str(dataset_dir),
        "stem_dir": str(stem_dir),
        "pred_dir": str(pred_dir),
        "pred_run": pred_dir.name,
        "regenerate_command": REGENERATE_CMD,
        "n_songs_total": len(songs),
        "n_predictions_available": sum(1 for e in entries if e["has_prediction"]),
        "n_songs_scored": len(scored),
        "n_songs_error": sum(1 for e in entries if e["error"] is not None),
        "n_words_scored": sum(len(e["words"]) for e in scored),
        "n_displaced_to_repeat": sum(e["n_displaced_to_repeat"] for e in entries),
        "n_displaced_by_phrase": sum(e["n_displaced_by_phrase"] for e in entries),
        "displace_tol_s": DISPLACE_TOL_S,
        "phrase_offset_tol_s": PHRASE_OFFSET_TOL_S,
        "phrase_min_run": PHRASE_MIN_RUN,
        "pooled": pooled,
        "songs": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="lyrics_align_manifest.py", description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR)
    parser.add_argument("--pred", type=Path, default=DEFAULT_PRED_DIR, help="predictions dir")
    parser.add_argument("--stems", type=Path, default=DEFAULT_STEM_DIR, help="vocal stems dir")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="manifest json to write")
    args = parser.parse_args()

    manifest = build_manifest(args.dataset_dir, args.pred, args.stems)
    args.out.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    size_mb = args.out.stat().st_size / 1e6
    print(
        f"[OK] {args.out} ({size_mb:.2f} MB)\n"
        f"     {manifest['n_predictions_available']} of {manifest['n_songs_total']} predictions "
        f"available, {manifest['n_songs_scored']} scored, {manifest['n_songs_error']} errored"
    )
    if manifest["n_songs_total"] != EXPECTED_SONGS:
        print(f"[WARN] dataset holds {manifest['n_songs_total']} songs, expected {EXPECTED_SONGS}")
    if manifest["pooled"] is not None:
        pooled = manifest["pooled"]
        print(
            f"     pooled n={pooled['n_words']} words  AAE {pooled['aae_s']:.3f}s  "
            f"med {pooled['median_s']:.3f}s  @100ms {pooled['acc']['100ms']:.1%}  "
            f"@300ms {pooled['acc']['300ms']:.1%}"
        )
        displaced = manifest["n_displaced_to_repeat"]
        print(
            f"     displaced to a repeat: {displaced} words "
            f"({displaced / manifest['n_words_scored']:.1%} of scored words) land within "
            f"{DISPLACE_TOL_S}s of another occurrence of the same word"
        )
        by_phrase = manifest["n_displaced_by_phrase"]
        print(
            f"     displaced with their phrase: {by_phrase} more words "
            f"({by_phrase / manifest['n_words_scored']:.1%} of scored words) moved together "
            f"with a majority-displaced run of {PHRASE_MIN_RUN}+ words"
        )
    for entry in manifest["songs"]:
        if entry["error"] is not None:
            print(f"[ERROR] {entry['name']}: {entry['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
