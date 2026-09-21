"""Lyric-sources CLI: availability matrix + text fetch across providers.

  uv run --no-sync python -m apps.lyrics.sources check --tier free --present-only
  uv run --no-sync python -m apps.lyrics.sources check --provider musixmatch --sample 100
  uv run --no-sync python -m apps.lyrics.sources fetch --provider lrclib --present-only
  uv run --no-sync python -m apps.lyrics.sources summary

Per-provider JSONL ledgers under data/state/lyrics-eval/source-availability/
are resumable (a track already in the ledger is never re-queried), and keyed
providers hard-stop at their daily budget -- rerun tomorrow to continue.
Fetched text lands in data/state/lyrics-eval/lyrics-text/<provider>/<key>.txt
(musixmatch = ~30% previews by plan, named preview-<key>.txt).
"""

from __future__ import annotations

import argparse
import json
import sys

from apps.lyrics.sources.base import (
    STATE_DIR,
    BudgetExhausted,
    Provider,
    Track,
    polite_sleep,
)
from apps.lyrics.sources.library import collapse_duplicates, load_tracks
from apps.lyrics.sources.lrclib import LrclibProvider

AVAIL_DIR = STATE_DIR / "source-availability"
TEXT_DIR = STATE_DIR / "lyrics-text"

#-----------------------------------------------------------------------------


def _make_providers(names: list[str]) -> list[Provider]:
    providers: list[Provider] = []
    for name in names:
        if name == "lrclib":
            providers.append(LrclibProvider())
        elif name == "musixmatch":
            from apps.lyrics.sources.musixmatch import (
                MusixmatchProvider,  # keyed: init fails fast without key
            )

            providers.append(MusixmatchProvider())
        else:
            raise SystemExit(f"[ERROR] unknown provider {name!r}; known: lrclib, musixmatch")
    return providers


def _select_tracks(args: argparse.Namespace) -> list[Track]:
    tracks = load_tracks()
    n_all = len(tracks)
    tracks = collapse_duplicates(tracks)
    n_dupes = n_all - len(tracks)
    if args.present_only:
        tracks = [t for t in tracks if t.audio_path is not None]
    print(f"[..] denominator: {len(tracks)} unique tracks"
          f" ({'audio-resolved, ' if args.present_only else ''}"
          f"{n_dupes} playlist duplicates collapsed, {n_all} rows in export)")
    if args.sample:
        tracks = tracks[: args.sample]
    return tracks


def _ledger_load(provider: str) -> dict[str, dict]:
    path = AVAIL_DIR / f"{provider}.jsonl"
    rows: dict[str, dict] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            rows[row["key"]] = row
    return rows


def _cmd_check(args: argparse.Namespace) -> int:
    tracks = _select_tracks(args)
    for provider in _make_providers(args.providers):
        done = _ledger_load(provider.name)
        todo = [t for t in tracks if t.key not in done]
        print(f"[..] {provider.name}: {len(todo)} to check ({len(done)} in ledger)")
        AVAIL_DIR.mkdir(parents=True, exist_ok=True)
        with (AVAIL_DIR / f"{provider.name}.jsonl").open("a", encoding="utf-8") as out:
            for i, track in enumerate(todo, 1):
                try:
                    r = provider.check(track)
                except BudgetExhausted as e:
                    print(f"[STOP] {e}")
                    break
                out.write(json.dumps({
                    "key": track.key, "artist": track.artist, "title": track.title,
                    "found": r.found, "synced": r.synced, "instrumental": r.instrumental,
                    "detail": r.detail, "audio_present": track.audio_path is not None,
                }, ensure_ascii=False) + "\n")
                out.flush()
                marker = "SYNC" if r.synced else ("hit " if r.found else
                         ("inst" if r.instrumental else "MISS"))
                print(f"[{marker}] {provider.name} {i}/{len(todo)} "
                      f"{track.artist[:28]} - {track.norm_title[:40]}")
                polite_sleep(provider)
    return _cmd_summary(args)


def _cmd_fetch(args: argparse.Namespace) -> int:
    (provider,) = _make_providers(args.providers)
    tracks = _select_tracks(args)
    avail = _ledger_load(provider.name)
    # musixmatch Basic v2 returns FULL lyrics (probed Fri 28 Aug 2026), so no
    # preview- prefix distinguishes partial text any more
    prefix = ""
    out_dir = TEXT_DIR / provider.name
    out_dir.mkdir(parents=True, exist_ok=True)
    fetched = skipped = missing = 0
    for track in tracks:
        known = avail.get(track.key)
        if known is not None and not known["found"]:
            continue  # checked and absent -- no fetch call to waste
        out_path = out_dir / f"{prefix}{track.key}.txt"
        if out_path.exists():
            skipped += 1
            continue
        try:
            # fetch_pair (plain + synced LRC in one call) where the provider has
            # it; the synced text is a timing prior for the version-verifier
            # thread and lands as <key>.lrc next to the plain <key>.txt.
            synced: str | None = None
            if hasattr(provider, "fetch_pair"):
                text, synced = provider.fetch_pair(track)
            else:
                text = provider.fetch(track)
        except BudgetExhausted as e:
            print(f"[STOP] {e}")
            break
        if text is None and synced is None:
            missing += 1
        else:
            if text is not None:
                out_path.write_text(text, encoding="utf-8")
            if synced is not None:
                out_path.with_suffix(".lrc").write_text(synced, encoding="utf-8")
            fetched += 1
            print(f"[OK] {track.artist[:28]} - {track.norm_title[:40]} -> "
                  f"{out_path.name}{' + .lrc' if synced is not None else ''}")
        polite_sleep(provider)
    print(f"[DONE] fetched {fetched}, already-had {skipped}, no-text {missing} -> {out_dir}")
    return 0


def _cmd_candidates(args: argparse.Namespace) -> int:
    from apps.lyrics.sources.candidates import (
        CANDIDATES_DIR,
        lrclib_candidates,
        write_candidate_set,
    )

    (provider,) = _make_providers(args.providers)
    if not isinstance(provider, LrclibProvider):
        raise SystemExit("[ERROR] candidates currently implements lrclib only")
    tracks = _select_tracks(args)
    done_keys = {p.stem for p in CANDIDATES_DIR.glob("*.json")} if CANDIDATES_DIR.is_dir() else set()
    todo = [t for t in tracks if t.key not in done_keys]
    print(f"[..] candidate sets: {len(todo)} to build ({len(done_keys)} exist)")
    n_with = n_without = n_instr = 0
    for i, track in enumerate(todo, 1):
        cands, instr = lrclib_candidates(provider, track)
        write_candidate_set(track, cands, instr)
        if cands:
            n_with += 1
        elif instr:
            n_instr += 1
        else:
            n_without += 1
        marker = "ok" if cands else ("in" if instr else "--")
        print(f"[{marker} n={len(cands)}] {i}/{len(todo)} "
              f"{track.artist[:28]} - {track.norm_title[:40]}")
        polite_sleep(provider)
    print(f"[DONE] {n_with} with candidates, {n_instr} instrumental-declared, "
          f"{n_without} explicit no-source -> {CANDIDATES_DIR}")
    return 0


def _cmd_merge_musixmatch(_args: argparse.Namespace) -> int:
    """Merge the musixmatch ledger + fetched text into the candidate contract:
    licensed full-text candidates for matcher hits, instrumental_evidence for
    matcher instrumental flags. Idempotent -- re-running never duplicates."""
    from datetime import UTC, datetime

    from apps.lyrics.sources.candidates import CANDIDATES_DIR, validate_candidate_file

    ledger = _ledger_load("musixmatch")
    text_dir = TEXT_DIR / "musixmatch"
    now = datetime.now(UTC).isoformat(timespec="seconds")
    added = evidence = corroborated_no_text = skipped = 0
    for path in sorted(CANDIDATES_DIR.glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        key = d["track"]["key"]
        row = ledger.get(key)
        if row is None:
            continue
        changed = False
        track_id = row["detail"].removeprefix("track_id:") if row["detail"].startswith("track_id:") else None
        if row["found"] and not any(c["source"] == "musixmatch" for c in d["candidates"]):
            text_path = text_dir / f"{key}.txt"
            if text_path.is_file():
                rank = max((c["rank"] for c in d["candidates"]), default=-1) + 1
                d["candidates"].append({
                    "id": f"musixmatch-matcher-get-{rank}",
                    "source": "musixmatch", "method": "matcher-get", "rank": rank,
                    "plain_text": text_path.read_text(encoding="utf-8"),
                    "synced_lrc": None,
                    "source_track": {"artist": None, "title": None, "album": None,
                                     "duration_s": None, "source_id": track_id},
                    "duration_delta_s": None,
                    "retrieved_at": now,
                })
                added += 1
                changed = True
            else:
                corroborated_no_text += 1
        elif row["found"]:
            skipped += 1
        if row["instrumental"] and not any(
            ev.get("source") == "musixmatch" for ev in d.get("instrumental_evidence", [])
        ):
            d.setdefault("instrumental_evidence", []).append({
                "source": "musixmatch", "method": "matcher-track-get",
                "source_id": track_id, "retrieved_at": now,
            })
            evidence += 1
            changed = True
        if changed:
            validate_candidate_file(d, expected_key=key)
            path.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[DONE] musixmatch merge: {added} candidates added, {evidence} instrumental-evidence "
          f"entries, {skipped} already merged, {corroborated_no_text} found-but-no-text-file")
    return 0


def _cmd_recover(args: argparse.Namespace) -> int:
    """Relaxed-query recovery over EMPTY, non-instrumental candidate files:
    measures how much of the no-source residual is our own matching's fault
    (feat clauses, non-remix suffixes) rather than a genuinely absent source."""
    from apps.lyrics.sources.candidates import (
        CANDIDATES_DIR,
        relaxed_recovery_candidates,
        write_candidate_set,
    )

    provider = LrclibProvider()
    tracks = {t.key: t for t in _select_tracks(args)}
    todo = []
    for path in sorted(CANDIDATES_DIR.glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        if d.get("instrumental_evidence") or d["candidates"]:
            continue
        if d["track"]["key"] in tracks:
            todo.append(tracks[d["track"]["key"]])
    if args.sample:
        todo = todo[: args.sample]
    print(f"[..] relaxed recovery over {len(todo)} no-source tracks")
    recovered = 0
    for i, track in enumerate(todo, 1):
        cands = relaxed_recovery_candidates(provider, track)
        if cands:
            write_candidate_set(track, cands)
            recovered += 1
            print(f"[ok n={len(cands)}] {i}/{len(todo)} "
                  f"{track.artist[:28]} - {track.norm_title[:40]}")
        else:
            print(f"[--  ] {i}/{len(todo)} {track.artist[:28]} - {track.norm_title[:40]}")
        polite_sleep(provider)
    print(f"[DONE] relaxed queries recovered {recovered}/{len(todo)} no-source tracks")
    return 0


def _cmd_crosscheck(_args: argparse.Namespace) -> int:
    """One honest view: candidate-bucket partition x vocal-presence verdicts,
    with the contradiction lists. Fails fast if the buckets do not partition."""
    from apps.lyrics.sources.candidates import CANDIDATES_DIR, bucket_of

    verdicts: dict[str, str] = {}
    vdir = STATE_DIR / "vocal-presence"
    for path in vdir.glob("*.json") if vdir.is_dir() else []:
        row = json.loads(path.read_text(encoding="utf-8"))
        verdicts[row["track_key"]] = row["verdict"]

    files = sorted(CANDIDATES_DIR.glob("*.json"))
    buckets: dict[str, int] = {}
    matrix: dict[tuple[str, str], int] = {}
    declared_with_candidates = 0
    contra_declared_vocal: list[str] = []
    contra_candidates_instr: list[str] = []
    for path in files:
        d = json.loads(path.read_text(encoding="utf-8"))
        b = bucket_of(d)
        buckets[b] = buckets.get(b, 0) + 1
        if b == "instrumental-declared" and d["candidates"]:
            declared_with_candidates += 1
        v = verdicts.get(d["track"]["key"], "unclassified")
        matrix[(b, v)] = matrix.get((b, v), 0) + 1
        label = f'{d["track"]["artist"]} - {d["track"]["title"]}'
        if b == "instrumental-declared" and v == "vocal":
            contra_declared_vocal.append(label)
        elif b in ("has-candidates", "genius-only") and v == "instrumental":
            contra_candidates_instr.append(label)

    total = len(files)
    if sum(buckets.values()) != total:
        raise SystemExit(f"[ERROR] buckets sum {sum(buckets.values())} != {total} files")
    covered = buckets.get("has-candidates", 0) + buckets.get("genius-only", 0)
    lyric_needing = total - buckets.get("instrumental-declared", 0)
    print(f"\nCandidate buckets over {total} tracks (partition checked):")
    for b in ("has-candidates", "genius-only", "instrumental-declared", "no-source"):
        print(f"  {b:22s} {buckets.get(b, 0):5d}")
    print(f"  ({declared_with_candidates} instrumental-declared files also hold candidates; "
          f"declaration wins for routing, verifier arbitrates)")
    strict = buckets.get("has-candidates", 0)
    print(f"  HONEST coverage: {covered}/{lyric_needing} lyric-needing = {covered / lyric_needing:.1%}")
    print(f"  STRICT coverage (excl. genius-only; scrape precision measured ~24%, "
          f"Fri 28 Aug 2026): {strict}/{lyric_needing} = {strict / lyric_needing:.1%}")
    print(f"\nBucket x vocal-presence verdict ({len(verdicts)} verdicts on disk):")
    for (b, v), n in sorted(matrix.items()):
        print(f"  {b:22s} {v:12s} {n:5d}")
    print("\nContradictions (verdicts are EVIDENCE, AUC 0.96, not truth):")
    print(f"  declared-instrumental but stem says vocal: {len(contra_declared_vocal)}")
    for x in contra_declared_vocal:
        print(f"    {x}")
    print(f"  has-candidates but stem says instrumental (wrong-match suspects): "
          f"{len(contra_candidates_instr)}")
    for x in contra_candidates_instr:
        print(f"    {x}")
    report_path = STATE_DIR / "crosscheck-report.json"
    report_path.write_text(json.dumps({
        "buckets": buckets, "total": total, "covered": covered,
        "lyric_needing": lyric_needing,
        "verdict_matrix": {f"{b}|{v}": n for (b, v), n in sorted(matrix.items())},
        "declared_instrumental_but_vocal_stem": contra_declared_vocal,
        "has_candidates_but_instrumental_stem": contra_candidates_instr,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[OK] report -> {report_path}")
    return 0


def _cmd_validate(_args: argparse.Namespace) -> int:
    from apps.lyrics.sources.candidates import CANDIDATES_DIR, validate_candidate_file

    files = sorted(CANDIDATES_DIR.glob("*.json"))
    bad = 0
    for path in files:
        try:
            validate_candidate_file(
                json.loads(path.read_text(encoding="utf-8")), expected_key=path.stem)
        except ValueError as e:
            bad += 1
            print(f"[BAD] {path.name}: {e}")
    print(f"[{'ERROR' if bad else 'OK'}] {len(files) - bad}/{len(files)} candidate files valid")
    return 1 if bad else 0


def _cmd_summary(args: argparse.Namespace) -> int:
    print()
    print("Availability matrix (per provider ledger; denominators differ by run scope):")
    for path in sorted(AVAIL_DIR.glob("*.jsonl")):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        n = len(rows)
        if n == 0:
            continue
        present = [r for r in rows if r.get("audio_present")]
        found = sum(r["found"] for r in rows)
        synced = sum(r["synced"] for r in rows)
        p_found = sum(r["found"] for r in present)
        print(f"  {path.stem:12s} checked {n:5d}  found {found:5d} ({found / n:5.1%})  "
              f"synced {synced:5d}  | audio-present subset: {p_found}/{len(present)}"
              f" = {p_found / len(present):5.1%}" if present else "")
    # the ledger view alone under-reports (it predates remix/genius/recovery
    # passes); the bucket partition is the real scoreboard
    return _cmd_crosscheck(args)


def _default_providers(tier: str) -> list[str]:
    tier_map = {"free": ["lrclib"], "keyed": ["musixmatch"], "paid": [], "all": ["lrclib", "musixmatch"]}
    providers = tier_map[tier]
    if not providers:
        raise SystemExit(f"[ERROR] no providers in tier {tier!r}")
    return providers


def _dispatch_command(args: argparse.Namespace) -> int:
    if args.command == "check":
        return _cmd_check(args)
    if args.command == "fetch":
        if len(args.providers) != 1:
            raise SystemExit("[ERROR] fetch takes exactly one --provider")
        return _cmd_fetch(args)
    if args.command == "candidates":
        if len(args.providers) != 1:
            raise SystemExit("[ERROR] candidates takes exactly one --provider")
        return _cmd_candidates(args)
    if args.command == "recover":
        return _cmd_recover(args)
    if args.command == "merge-musixmatch":
        return _cmd_merge_musixmatch(args)
    if args.command == "crosscheck":
        return _cmd_crosscheck(args)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "summary":
        return _cmd_summary(args)
    raise AssertionError(f"unhandled command {args.command!r}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m apps.lyrics.sources")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "fetch", "candidates", "recover",
                 "merge-musixmatch", "crosscheck", "validate", "summary"):
        p = sub.add_parser(name)
        p.add_argument("--provider", dest="providers", action="append", default=None,
                       help="repeatable; default: all in --tier")
        p.add_argument("--tier", choices=("free", "keyed", "paid", "all"), default="free")
        p.add_argument("--present-only", action="store_true",
                       help="only tracks with locally resolved audio (honest karaoke denominator)")
        p.add_argument("--sample", type=int, default=None)
    args = parser.parse_args()

    if args.providers is None:
        args.providers = _default_providers(args.tier)
    return _dispatch_command(args)


if __name__ == "__main__":
    sys.exit(main())
