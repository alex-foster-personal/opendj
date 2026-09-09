"""Lyrics CLI: line-level fetch/index, plus the word-level alignment evals.

Shipping side (LYRICS-01/03):
- ``fetch <stable_id>`` fetches and caches one track's line-synced lyrics.
- ``index`` builds or resumes the durable checkpointed lyric-search index by
  draining one bounded batch at a time until nothing is left to index;
  ``--once`` runs a single batch and reports, which is what a caller drives
  when it wants to control pausing itself. ``--force-rebuild`` is the
  reachable recovery path for the bulk-removal guard (issue #1343 round-3
  review, ``apps.lyrics.search_index.MAX_REMOVAL_FRACTION``): the background
  watcher never rebuilds on its own (that would defeat the guard), so an
  operator who has confirmed a shrunk cache is real, not a mount/permissions
  problem, runs this flag once to let the batch through.

Eval side (specs/karaoke-lyrics-alignment.md, landed by section 13 D13.4):
- ``stats`` dataset summary (songs per language, word counts) -- proves the
  ground truth loads.
- ``score --pred DIR`` scores predicted alignments (one ``<song_name>.json``
  per song) against JamendoLyrics word onsets.
- ``crosscheck --pred --asr`` round-4a flag eval (precision/recall/lift of
  ASR-disagreement flags).
- ``witness-eval --pred --asr`` round-5 calibration of the witness CLASSES
  (the listen page's dot colors) against ground truth: P(error | class),
  false-red rate, green error rate.

The eval subcommands take ``--dataset-dir`` themselves rather than through a
parser-wide flag, so fetch/index never advertise an option that means nothing
to them.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from apps.lyrics.annotations import load_annotations, validate_against_songs
from apps.lyrics.crosscheck import crosscheck, flagged_indices, witness_verdicts
from apps.lyrics.jamendo import DEFAULT_DATASET_DIR, JamendoSong, load_songs
from apps.lyrics.metrics import OnsetErrorReport, aggregate, format_report, score_onsets
from apps.lyrics.search_index import index_batch, index_path
from apps.lyrics.service import LyricsService
from apps.shared.paths import DATA_DIR

WITNESS_CLASS_ORDER = ("agree", "drift", "contradict", "lost", "unheard", "unmatchable")
WITNESS_RED_CLASSES = frozenset({"contradict", "lost"})

#-----------------------------------------------------------------------------
# parser
#-----------------------------------------------------------------------------


def _add_dataset_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=DEFAULT_DATASET_DIR,
        help="JamendoLyrics ground-truth dataset root",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.lyrics")
    subcommands = parser.add_subparsers(dest="command", required=True)

    fetch = subcommands.add_parser("fetch", help="fetch and cache line-synced lyrics")
    fetch.add_argument("track", help="stable track id")
    fetch.add_argument("--data-dir", type=Path, default=DATA_DIR, help="data root")

    index = subcommands.add_parser(
        "index", help="build or resume the checkpointed lyric-search index"
    )
    index.add_argument("--data-dir", type=Path, default=DATA_DIR, help="data root")
    index.add_argument(
        "--once",
        action="store_true",
        help="run one bounded batch and report, instead of draining to idle",
    )
    index.add_argument(
        "--batch",
        type=int,
        default=100,
        help="max documents indexed per batch (one poll of the job)",
    )
    index.add_argument(
        "--interval",
        type=float,
        default=0.2,
        help="seconds to pause between batches while draining",
    )
    index.add_argument(
        "--force-rebuild",
        action="store_true",
        help=(
            "reconcile a bulk removal (more than MAX_REMOVAL_FRACTION of the "
            "indexed rows gone) that the guard would otherwise refuse; only "
            "the first batch of this run applies it, so a real bulk removal "
            "does not disable the guard for the rest of the drain"
        ),
    )

    stats = subcommands.add_parser("stats", help="ground-truth dataset summary")
    _add_dataset_dir(stats)

    score = subcommands.add_parser("score", help="score predicted alignments")
    _add_dataset_dir(score)
    score.add_argument(
        "--pred", type=Path, required=True, help="dir of <song_name>.json predictions"
    )
    score.add_argument(
        "--by-tag",
        action="store_true",
        help=(
            "also pool metrics per failure-mode tag from "
            "apps/lyrics/annotations/jamendolyrics.yaml"
        ),
    )

    cc = subcommands.add_parser("crosscheck", help="eval ASR-disagreement flags vs ground truth")
    _add_dataset_dir(cc)
    cc.add_argument("--pred", type=Path, required=True, help="aligner prediction dir")
    cc.add_argument("--asr", type=Path, required=True, help="ASR dir (modal_asr_spike)")
    cc.add_argument(
        "--delta", type=float, default=0.5, help="flag threshold on |aligner - asr| seconds"
    )
    cc.add_argument(
        "--error-tol",
        type=float,
        default=0.3,
        help="ground-truth error definition |pred - gt| seconds",
    )
    cc.add_argument(
        "--min-unmatched-run",
        type=int,
        default=5,
        help="flag unmatched words only in runs >= N (0 = never)",
    )

    we = subcommands.add_parser(
        "witness-eval", help="calibrate witness classes (dot colors) against ground truth"
    )
    _add_dataset_dir(we)
    we.add_argument("--pred", type=Path, required=True, help="aligner prediction dir")
    we.add_argument("--asr", type=Path, required=True, help="ASR transcript dir")
    we.add_argument(
        "--error-tol",
        type=float,
        default=0.3,
        help="ground-truth error definition |pred - gt| seconds",
    )
    we.add_argument(
        "--local-window",
        type=float,
        default=3.0,
        help="local same-token match window seconds (0 = round-4 diff-only)",
    )
    return parser


#-----------------------------------------------------------------------------
# shipping commands
#-----------------------------------------------------------------------------


def _cmd_fetch(data_dir: Path, track: str) -> int:
    lyrics = LyricsService(data_dir).fetch_stable_id(track)
    print(
        json.dumps(
            {
                "stable_id": lyrics.stable_id,
                "source": lyrics.source,
                "lines": [
                    {"start_ms": line.start_ms, "text": line.text} for line in lyrics.lines
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_index(args: argparse.Namespace) -> int:
    batch = index_batch(args.data_dir, max_docs=args.batch, force_rebuild=args.force_rebuild)
    while not args.once and not batch.done:
        time.sleep(max(0.0, args.interval))
        # force_rebuild applies only to the first batch above: the bulk
        # removal it was for is reconciled by then, and re-arming the
        # guard for the rest of the drain is what keeps a real future
        # bulk removal from sailing through unnoticed.
        batch = index_batch(args.data_dir, max_docs=args.batch)
    print(
        json.dumps(
            {
                "indexed_file": str(index_path(args.data_dir)),
                "done": batch.done,
                "docs_indexed": batch.docs_indexed,
            },
            ensure_ascii=False,
        )
    )
    return 0


#-----------------------------------------------------------------------------
# eval commands
#-----------------------------------------------------------------------------


def _pred_words(path: Path) -> list[dict]:
    """The ``words`` array of one prediction or ASR JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))["words"]


def _song_key(name: str) -> str:
    """Join key for a song name, normalization-independent.

    A prediction file's name comes back from the filesystem in whatever
    unicode form the volume stored (APFS hands back NFD here), while the
    dataset CSV holds NFC. Joining the raw strings drops the 11 accented
    songs of 79 -- a silently shrunk denominator, which is exactly the
    failure the house rule forbids. Path LOOKUPS are unaffected (APFS
    compares normalization-insensitively); only dict joins need this.
    """
    return unicodedata.normalize("NFC", name)


def _cmd_stats(dataset_dir: Path) -> int:
    songs = load_songs(dataset_dir)
    by_language = Counter(s.language for s in songs)
    total_words = sum(len(s.words) for s in songs)
    print(f"songs: {len(songs)}   words: {total_words}")
    for language, n in sorted(by_language.items()):
        n_words = sum(len(s.words) for s in songs if s.language == language)
        print(f"  {language:<8s} {n:>3d} songs  {n_words:>6d} words")
    flagged = [s.name for s in songs if s.lyric_overlap or s.polyphonic or s.non_lexical]
    print(f"flagged (overlap/polyphonic/non-lexical): {len(flagged)}")
    return 0


def _print_tag_slices(songs: list[JamendoSong], pred_files: dict[str, Path]) -> None:
    """Pool word errors per failure-mode tag (plus the untagged complement).

    Tagged indices come from apps/lyrics/annotations/jamendolyrics.yaml; each
    tag row is word-pooled exactly across every scored song, so every failure
    mode gets its own denominator.
    """
    annotations = load_annotations()
    validate_against_songs(annotations, {s.name: len(s.words) for s in songs})
    by_tag: dict[str, tuple[list[float], list[float]]] = {}
    untagged: tuple[list[float], list[float]] = ([], [])
    for song in songs:
        pred_path = pred_files.get(_song_key(song.name))
        if pred_path is None:
            continue
        pred_starts = [w["start_s"] for w in _pred_words(pred_path)]
        tagged_idx: set[int] = set()
        for span in annotations.get(song.name, []):
            ref_acc, pred_acc = by_tag.setdefault(span.tag, ([], []))
            for i in span.word_indices(len(song.words)):
                ref_acc.append(song.words[i].start_s)
                pred_acc.append(pred_starts[i])
                tagged_idx.add(i)
        for i in range(len(song.words)):
            if i not in tagged_idx:
                untagged[0].append(song.words[i].start_s)
                untagged[1].append(pred_starts[i])
    print()
    print("By failure-mode tag (apps/lyrics/annotations/jamendolyrics.yaml):")
    for tag in sorted(by_tag):
        ref_acc, pred_acc = by_tag[tag]
        print(format_report(f"  tag:{tag}", score_onsets(ref_acc, pred_acc)))
    print(format_report("  (untagged words)", score_onsets(untagged[0], untagged[1])))


def _cmd_score(dataset_dir: Path, pred_dir: Path, by_tag: bool) -> int:
    songs = load_songs(dataset_dir)
    pred_files = {_song_key(p.stem): p for p in pred_dir.glob("*.json")}
    if not pred_files:
        print(f"[ERROR] no *.json predictions in {pred_dir}", file=sys.stderr)
        return 1
    unknown = sorted(set(pred_files) - {_song_key(s.name) for s in songs})
    if unknown:
        print(f"[ERROR] predictions for unknown songs: {unknown}", file=sys.stderr)
        return 1

    reports: list[OnsetErrorReport] = []
    for song in songs:
        pred_path = pred_files.get(_song_key(song.name))
        if pred_path is None:
            continue
        report = score_onsets(song.word_starts_s, [w["start_s"] for w in _pred_words(pred_path)])
        reports.append(report)
        print(format_report(song.name, report))
    print("-" * 120)
    print(format_report(f"POOLED ({len(reports)}/{len(songs)} songs)", aggregate(reports)))
    if len(reports) < len(songs):
        missing = len(songs) - len(reports)
        print(
            f"[WARN] {missing} songs had no prediction file "
            f"(denominator: {len(reports)} scored)"
        )
    if by_tag:
        _print_tag_slices(songs, pred_files)
    return 0


def _cmd_crosscheck(
    dataset_dir: Path,
    pred_dir: Path,
    asr_dir: Path,
    delta_s: float,
    error_tol_s: float,
    min_unmatched_run: int,
) -> int:
    """Round-4a eval: do ASR-disagreement flags predict REAL onset errors?

    Ground truth defines an "error word" as |pred - gt| > error_tol_s. Flags
    come from apps/lyrics/crosscheck.py (unmatched by ASR, or matched with
    |aligner - asr| > delta_s). Reports per-song and pooled precision/recall
    plus the lift over the base error rate (round 3a's self-score baseline for
    comparison: 3.2x lift, 31.6% recall at the bottom decile).
    """
    songs = load_songs(dataset_dir)
    header = (
        f"{'song':<44s} {'ver_sim':>7s} {'match':>6s} {'flags':>6s} "
        f"{'prec':>6s} {'recall':>6s}"
    )
    print(header)
    tp = fp = total_words = total_errors = scored = 0
    for song in songs:
        pred_path = pred_dir / f"{song.name}.json"
        asr_path = asr_dir / f"{song.name}.json"
        if not pred_path.is_file() or not asr_path.is_file():
            continue
        scored += 1
        s_tp, s_fp, n_words, n_errors = _print_crosscheck_song_row(
            song, pred_path, asr_path, delta_s, error_tol_s, min_unmatched_run
        )
        tp += s_tp
        fp += s_fp
        total_words += n_words
        total_errors += n_errors
    if not scored:
        print(
            f"[ERROR] no songs have both predictions ({pred_dir}) and ASR ({asr_dir})",
            file=sys.stderr,
        )
        return 1
    print("-" * len(header))
    _print_crosscheck_pooled(
        scored, len(songs), tp, fp, total_words, total_errors, delta_s, error_tol_s
    )
    return 0


def _print_crosscheck_song_row(
    song: JamendoSong,
    pred_path: Path,
    asr_path: Path,
    delta_s: float,
    error_tol_s: float,
    min_unmatched_run: int,
) -> tuple[int, int, int, int]:
    """Score and print one song; return (true pos, false pos, words, errors)."""
    pred_starts = [w["start_s"] for w in _pred_words(pred_path)]
    words = [w.word for w in song.words]
    report = crosscheck(words, pred_starts, _pred_words(asr_path))
    flags = flagged_indices(report, delta_s, min_unmatched_run=min_unmatched_run)
    errors = {
        i
        for i, (p, g) in enumerate(zip(pred_starts, song.word_starts_s, strict=True))
        if abs(p - g) > error_tol_s
    }
    s_tp = len(flags & errors)
    prec = s_tp / len(flags) if flags else float("nan")
    rec = s_tp / len(errors) if errors else float("nan")
    print(
        f"{song.name[:44]:<44s} {report.version_similarity:>7.2f} "
        f"{report.match_ratio:>6.1%} {len(flags):>6d} {prec:>6.1%} {rec:>6.1%}"
    )
    return s_tp, len(flags - errors), len(words), len(errors)


def _print_crosscheck_pooled(
    scored: int,
    n_songs: int,
    tp: int,
    fp: int,
    total_words: int,
    total_errors: int,
    delta_s: float,
    error_tol_s: float,
) -> None:
    """Pooled precision/recall/lift over the songs that actually scored."""
    n_flagged = tp + fp
    base_rate = total_errors / total_words
    precision = tp / n_flagged if n_flagged else float("nan")
    recall = tp / total_errors if total_errors else float("nan")
    lift = precision / base_rate if n_flagged and base_rate else float("nan")
    print(
        f"POOLED ({scored}/{n_songs} songs, {total_words} words, "
        f"error=|pred-gt|>{error_tol_s:.1f}s, flag delta>{delta_s:.1f}s)"
    )
    print(
        f"  base error rate {base_rate:.1%}  flagged {n_flagged} "
        f"({n_flagged / total_words:.1%})  precision {precision:.1%}  "
        f"recall {recall:.1%}  lift {lift:.1f}x"
    )
    if scored < n_songs:
        print(
            f"[WARN] {n_songs - scored} songs missing pred or asr "
            f"(denominator: {scored} scored)"
        )


def _cmd_witness_eval(
    dataset_dir: Path,
    pred_dir: Path,
    asr_dir: Path,
    error_tol_s: float,
    local_window_s: float,
) -> int:
    """Round-5 calibration: what does each witness CLASS mean against ground truth?

    The listen page paints dot colors from these classes, so this table is the
    page's honesty check: P(error | class) per class, plus the two product
    numbers -- false-red rate (share of CORRECT words painted red: the
    Jamming-verse complaint) and green error rate (share of green-lit words
    that are wrong: the page's implicit promise). --local-window 0 reproduces
    the round-4 diff-only matcher for A/B.
    """
    songs = load_songs(dataset_dir)
    per_class: dict[str, list[int]] = {c: [0, 0] for c in WITNESS_CLASS_ORDER}  # [n, n_error]
    scored = total_words = total_errors = red_on_correct = 0
    for song in songs:
        pred_path = pred_dir / f"{song.name}.json"
        asr_path = asr_dir / f"{song.name}.json"
        if not pred_path.is_file() or not asr_path.is_file():
            continue
        scored += 1
        pred_starts = [w["start_s"] for w in _pred_words(pred_path)]
        verdicts = witness_verdicts(
            [w.word for w in song.words],
            pred_starts,
            _pred_words(asr_path),
            local_window_s=local_window_s,
        )
        for v, pred, gt in zip(verdicts, pred_starts, song.word_starts_s, strict=True):
            is_error = abs(pred - gt) > error_tol_s
            per_class[v.verdict][0] += 1
            per_class[v.verdict][1] += int(is_error)
            total_words += 1
            total_errors += int(is_error)
            if v.verdict in WITNESS_RED_CLASSES and not is_error:
                red_on_correct += 1
    if not scored:
        print(
            f"[ERROR] no songs have both predictions ({pred_dir}) and ASR ({asr_dir})",
            file=sys.stderr,
        )
        return 1
    _print_witness_table(
        per_class,
        n_songs=len(songs),
        scored=scored,
        total_words=total_words,
        total_errors=total_errors,
        red_on_correct=red_on_correct,
        error_tol_s=error_tol_s,
        local_window_s=local_window_s,
    )
    return 0


def _print_witness_table(
    per_class: dict[str, list[int]],
    *,
    n_songs: int,
    scored: int,
    total_words: int,
    total_errors: int,
    red_on_correct: int,
    error_tol_s: float,
    local_window_s: float,
) -> None:
    window = "OFF (round-4 diff only)" if local_window_s == 0 else f"{local_window_s:.1f}s"
    print(
        f"WITNESS CALIBRATION ({scored}/{n_songs} songs, {total_words} words, "
        f"error=|pred-gt|>{error_tol_s:.1f}s, local window {window})"
    )
    print(f"{'class':<12s} {'words':>7s} {'share':>7s} {'P(error|class)':>15s}")
    for cls in WITNESS_CLASS_ORDER:
        n, n_err = per_class[cls]
        p_err = f"{n_err / n:.1%}" if n else "n/a"
        print(f"{cls:<12s} {n:>7d} {n / total_words:>7.1%} {p_err:>15s}")
    n_red = sum(per_class[c][0] for c in WITNESS_RED_CLASSES)
    n_red_err = sum(per_class[c][1] for c in WITNESS_RED_CLASSES)
    n_green, n_green_err = per_class["agree"]
    n_correct = total_words - total_errors
    print("-" * 44)
    print(f"base error rate     {total_errors / total_words:.1%}")
    print(
        f"red precision       {n_red_err / n_red:.1%}  recall "
        f"{n_red_err / total_errors:.1%}  ({n_red} red)"
    )
    print(f"FALSE-RED rate      {red_on_correct / n_correct:.1%} of correct words painted red")
    print(f"green error rate    {n_green_err / n_green:.1%} of green-lit words are wrong")


#-----------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "fetch":
        return _cmd_fetch(args.data_dir, args.track)
    elif args.command == "index":  # noqa: RET505 - explicit elif is the house style
        return _cmd_index(args)
    elif args.command == "stats":
        return _cmd_stats(args.dataset_dir)
    elif args.command == "score":
        return _cmd_score(args.dataset_dir, args.pred, args.by_tag)
    elif args.command == "crosscheck":
        return _cmd_crosscheck(
            args.dataset_dir,
            args.pred,
            args.asr,
            args.delta,
            args.error_tol,
            args.min_unmatched_run,
        )
    elif args.command == "witness-eval":
        return _cmd_witness_eval(
            args.dataset_dir, args.pred, args.asr, args.error_tol, args.local_window
        )
    else:
        raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
