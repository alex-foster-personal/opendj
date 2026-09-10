"""Score a JamendoLyrics predictions dir with the ratified Ship-tier scorer.

This is the #1515 LYR-01 evidence path (operational-plan D13.5): the aligner's
per-word onsets go through :func:`scripts.lyrics_alignment.scorer.lyric_align_score`
index-aligned to the reference words, with ``None`` for any word the aligner
did not place, and the run reports pooled + per-song medae_s, pco_300ms,
catastrophe_rate, recall and words_unplaced against a named denominator.

``--write-fixture`` emits the committed regression fixture: a stratified sample
of songs, TIMINGS ONLY. No lyric text and no song titles leave this script -
the alignment spike spec forbids committing lyrics, and track ids are opaque
digests of the song name so the fixture cannot be read back into a corpus.

    python -m scripts.lyrics_alignment.measure_jamendo \\
        --pred data/state/lyrics-eval/round3a-best-of \\
        [--dataset-dir DIR] [--write-fixture PATH] [--sample 10]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from apps.lyrics.jamendo import DEFAULT_DATASET_DIR, JamendoSong, load_songs
from scripts.lyrics_alignment.scorer import (
    SCORER_VERSION,
    SHIP_CATASTROPHE_RATE_MAX,
    SHIP_MEDAE_S_MAX,
    SHIP_PCO_300MS_MIN,
    LyricAlignScore,
    lyric_align_score,
)

FIXTURE_SET = "lyrics-alignment-jamendo-round3a-measured-v1"
DEFAULT_SAMPLE = 10
ONSET_DECIMALS = 4

#-----------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoredSong:
    """One song, the prediction file it was scored against, and its score."""

    song: JamendoSong
    pred_path: Path
    score: LyricAlignScore

    def predicted_onsets(self) -> list[float | None]:
        return _predicted_onsets(self.pred_path, len(self.song.words))


def _song_key(name: str) -> str:
    """NFC join key: APFS returns NFD filenames, the dataset CSV holds NFC."""
    return unicodedata.normalize("NFC", name)


def opaque_track_id(song_name: str) -> str:
    """A stable, opaque id for a song. Not reversible to a title."""
    return "jl-" + hashlib.sha256(_song_key(song_name).encode("utf-8")).hexdigest()[:12]


def _predicted_onsets(pred_path: Path, n_reference_words: int) -> list[float | None]:
    """Per-reference-word predicted onsets; None where the aligner placed nothing.

    A prediction file that does not cover every reference word is a hard error:
    a forced alignment that silently returned a different word count would make
    every index-aligned metric below meaningless.
    """
    words = json.loads(pred_path.read_text(encoding="utf-8"))["words"]
    if len(words) != n_reference_words:
        raise ValueError(
            f"{pred_path.name}: {len(words)} predicted words vs "
            f"{n_reference_words} reference words"
        )
    return [w.get("start_s") for w in words]


def score_predictions(
    songs: Sequence[JamendoSong], pred_dir: Path
) -> tuple[list[ScoredSong], LyricAlignScore, list[str]]:
    """Score every song that has a prediction; also pool every word exactly."""
    pred_files = {_song_key(p.stem): p for p in pred_dir.glob("*.json")}
    if not pred_files:
        raise FileNotFoundError(f"no *.json predictions in {pred_dir}")
    scored: list[ScoredSong] = []
    missing: list[str] = []
    pooled_reference: list[float] = []
    pooled_predicted: list[float | None] = []
    for song in songs:
        pred_path = pred_files.get(_song_key(song.name))
        if pred_path is None:
            missing.append(song.name)
            continue
        reference = song.word_starts_s
        predicted = _predicted_onsets(pred_path, len(reference))
        pooled_reference.extend(reference)
        pooled_predicted.extend(predicted)
        scored.append(
            ScoredSong(song, pred_path, lyric_align_score(reference, predicted))
        )
    return scored, lyric_align_score(pooled_reference, pooled_predicted), missing


def meets_ship_tier(score: LyricAlignScore) -> bool:
    return (
        score.medae_s <= SHIP_MEDAE_S_MAX
        and score.pco_300ms >= SHIP_PCO_300MS_MIN
        and score.catastrophe_rate <= SHIP_CATASTROPHE_RATE_MAX
    )


def stratified_sample(scored: Sequence[ScoredSong], size: int) -> list[ScoredSong]:
    """Even strides across the medae-sorted corpus, so the sample spans the
    real distribution (best, middle and worst songs) rather than the passes."""
    if size > len(scored):
        raise ValueError(f"sample {size} exceeds the {len(scored)} scored songs")
    ordered = sorted(scored, key=lambda row: row.score.medae_s)
    stride = (len(ordered) - 1) / (size - 1) if size > 1 else 0.0
    return [ordered[round(i * stride)] for i in range(size)]


#-----------------------------------------------------------------------------


def _print_report(
    scored: Sequence[ScoredSong],
    pooled: LyricAlignScore,
    missing: Sequence[str],
    n_songs: int,
) -> None:
    header = (
        f"{'song':<46s} {'medae_s':>8s} {'pco300':>7s} {'catas':>7s} "
        f"{'recall':>7s} {'unplaced':>8s} {'words':>6s}  ship"
    )
    print(header)
    for row in sorted(scored, key=lambda r: r.score.medae_s):
        song, score = row.song, row.score
        print(
            f"{song.name[:46]:<46s} {score.medae_s:>8.3f} {score.pco_300ms:>7.3f} "
            f"{score.catastrophe_rate:>7.3f} {score.recall:>7.3f} "
            f"{score.words_unplaced:>8d} {score.words_reference:>6d}  "
            f"{'PASS' if meets_ship_tier(score) else 'FAIL'}"
        )
    print("-" * len(header))
    label = f"POOLED ({len(scored)}/{n_songs} songs)"
    print(
        f"{label:<46s} {pooled.medae_s:>8.3f} {pooled.pco_300ms:>7.3f} "
        f"{pooled.catastrophe_rate:>7.3f} {pooled.recall:>7.3f} "
        f"{pooled.words_unplaced:>8d} {pooled.words_reference:>6d}  "
        f"{'PASS' if meets_ship_tier(pooled) else 'FAIL'}"
    )
    n_pass = sum(1 for row in scored if meets_ship_tier(row.score))
    print(
        f"Ship tier (medae <= {SHIP_MEDAE_S_MAX}, pco_300ms >= {SHIP_PCO_300MS_MIN}, "
        f"catastrophe <= {SHIP_CATASTROPHE_RATE_MAX}): {n_pass}/{len(scored)} songs"
    )
    if missing:
        print(f"[WARN] {len(missing)} songs had no prediction file: {sorted(missing)[:5]}")


def _measured(score: LyricAlignScore) -> dict:
    """One scored row as the fixture records it: verdict first, then metrics."""
    return {"meets_ship_tier": meets_ship_tier(score), **asdict(score)}


def _fixture_payload(
    sample: Sequence[ScoredSong],
    pooled: LyricAlignScore,
    pred_dir: Path,
    n_songs: int,
    n_scored: int,
) -> dict:
    # Score the ROUNDED onsets the fixture actually commits, so a reader who
    # recomputes from the file gets exactly the recorded numbers back.
    committed = [
        (
            row,
            [round(t, ONSET_DECIMALS) for t in row.song.word_starts_s],
            [None if t is None else round(t, ONSET_DECIMALS) for t in row.predicted_onsets()],
        )
        for row in sample
    ]
    sample_pooled = lyric_align_score(
        [onset for _, reference, _ in committed for onset in reference],
        [onset for _, _, predicted in committed for onset in predicted],
    )
    return {
        "fixture_set": FIXTURE_SET,
        "provenance": {
            "measured_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "scorer_version": SCORER_VERSION,
            "predictions": pred_dir.name,
            "dataset": "JamendoLyrics MultiLang word annotations",
            "sampling": (
                "even strides across the medae-sorted corpus, so the sample "
                "spans the real distribution rather than the passing songs"
            ),
            "note": (
                "Timings only. No lyric text and no song titles: track_id is an "
                "opaque digest of the song name."
            ),
        },
        "corpus_measured": {
            "denominator": f"{n_scored}/{n_songs} songs, {pooled.words_reference} words",
            **_measured(pooled),
        },
        "sample_pooled": {
            "denominator": (
                f"{len(sample)} sampled songs, {sample_pooled.words_reference} words"
            ),
            "warning": (
                "A regression pin, NOT an estimate of the corpus. Ten songs "
                "cannot reproduce a word-weighted 79-song pooling, and this "
                "sample can meet Ship tier while corpus_measured does not. "
                "Quote corpus_measured for the LYR-01 claim."
            ),
            **_measured(sample_pooled),
        },
        "tracks": [
            {
                "track_id": opaque_track_id(row.song.name),
                "measured": _measured(lyric_align_score(reference, predicted)),
                "reference_onsets_s": reference,
                "predicted_onsets_s": predicted,
            }
            for row, reference, predicted in committed
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.lyrics_alignment.measure_jamendo")
    parser.add_argument("--pred", type=Path, required=True, help="aligner prediction dir")
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR)
    parser.add_argument(
        "--write-fixture", type=Path, default=None, help="emit the timing-only fixture here"
    )
    parser.add_argument(
        "--sample", type=int, default=DEFAULT_SAMPLE, help="songs in the written fixture"
    )
    args = parser.parse_args(argv)

    songs = load_songs(args.dataset_dir)
    scored, pooled, missing = score_predictions(songs, args.pred)
    _print_report(scored, pooled, missing, len(songs))

    if args.write_fixture is not None:
        sample = stratified_sample(scored, args.sample)
        payload = _fixture_payload(sample, pooled, args.pred, len(songs), len(scored))
        args.write_fixture.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"[OK] wrote {len(sample)} timing-only tracks to {args.write_fixture}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
