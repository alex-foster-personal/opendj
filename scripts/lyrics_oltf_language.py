# /// script
# requires-python = ">=3.12"
# dependencies = ["lingua-language-detector>=2.0"]
# ///
"""OLTF language tagging from candidate lyric TEXT (round-4c method; --dir picks the corpus).

Reads data/state/lyrics-eval/oltf/tracks.json plus the fetch-fork-contract
candidate files in data/state/lyrics-eval/candidates/, runs lingua over the
SELECTED candidate's plain_text per track -- the version-screen's is_best
choice when data/state/lyrics-eval/oltf/version-screen.json has a row for the
track, rank-0 otherwise -- and writes data/state/lyrics-eval/oltf/language.json
with the chosen candidate_id recorded so make-jobs can assert it detected on
the same text it aligns. Run AFTER version-screen whenever candidates changed.

Mirrors round 4c exactly (scripts/lyrics_owncrate_spike.py): detector over ALL
75 lingua languages (never a narrowed allow-list, which would hide the exact
case the round exists to catch), decision floors MIN_CONFIDENCE 0.45 and
MIN_MARGIN 0.15. Below the floors the track is tagged "undecided" - recorded,
never guessed, resolved later by hand or by Whisper's acoustic language ID.
Zero-candidate tracks are tagged "no-text": text detection has nothing to read
and the acoustic pass is the only possible evidence.

Usage (repo root):
    uv run --script scripts/lyrics_oltf_language.py [--dir CORPUS]

--dir names the corpus dir under data/state/lyrics-eval/ (default oltf, so
nothing existing changes; e.g. --dir crate). Corpus rows with an
"excluded_reason" key are skipped with a printed count, mirroring
scripts/lyrics_oltf_spike.py.

-Claude
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from lingua import LanguageDetectorBuilder

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
LYRICS_EVAL_DIR: Path = REPO_ROOT / "data" / "state" / "lyrics-eval"
CORPUS_DIR: Path = LYRICS_EVAL_DIR / "oltf"  # reassigned from --dir in main()
CANDIDATES_DIR: Path = LYRICS_EVAL_DIR / "candidates"
MIN_CONFIDENCE: float = 0.45
MIN_MARGIN: float = 0.15


def _fork_key(artist: str, title: str, duration_s: int) -> str:
    """The fetch fork's Track.key derivation, duplicated verbatim (sha1 16-hex)."""
    return hashlib.sha1(f"{artist}|{title}|{duration_s}".encode()).hexdigest()[:16]


def _recover_artist_title(track: dict) -> tuple[str, str]:
    """Same recovery rule as scripts/lyrics_oltf_spike.py (basename, then title split)."""
    artist = (track["artist"] or "").strip()
    title = (track["title"] or "").strip()
    if artist:
        return artist, title
    basename = Path(track["path"]).stem if track["path"] else ""
    for source in (basename, title):
        if " - " in source:
            left, right = source.split(" - ", 1)
            return left.strip(), right.strip()
    return "", title


def _load_screen_best() -> dict[str, str]:
    """version-screen's is_best candidate_id per track_id ({} before the screen ran)."""
    path = CORPUS_DIR / "version-screen.json"
    if not path.is_file():
        return {}
    return {r["track_id"]: r["candidate_id"]
            for r in json.loads(path.read_text(encoding="utf-8")) if r.get("is_best")}


def main() -> int:
    global CORPUS_DIR
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default="oltf", metavar="CORPUS",
                    help="corpus dir name under data/state/lyrics-eval/ (default: oltf)")
    args = ap.parse_args()
    CORPUS_DIR = LYRICS_EVAL_DIR / args.dir
    if not CORPUS_DIR.is_dir():
        raise SystemExit(f"[ERROR] corpus dir {CORPUS_DIR} does not exist")
    out_path = CORPUS_DIR / "language.json"
    all_rows = json.loads((CORPUS_DIR / "tracks.json").read_text(encoding="utf-8"))
    excluded = [t for t in all_rows if t.get("excluded_reason")]
    if excluded:
        print(f"[..] {len(excluded)} of {len(all_rows)} corpus rows excluded upstream "
              f"(excluded_reason set)")
    tracks = [t for t in all_rows if not t.get("excluded_reason")]
    screen_best = _load_screen_best()
    detector = LanguageDetectorBuilder.from_all_languages().build()
    rows: list[dict] = []
    for t in tracks:
        artist, title = _recover_artist_title(t)
        key = _fork_key(artist, title, int(t["length_s"] or 0))
        cand_path = CANDIDATES_DIR / f"{key}.json"
        if not cand_path.is_file():
            raise SystemExit(f"[ERROR] no candidate file for {t['track_id']} ({key}) -- "
                             f"run scripts/lyrics_oltf_spike.py candidates first")
        cands = json.loads(cand_path.read_text(encoding="utf-8"))["candidates"]
        row = {"track_id": t["track_id"], "fetch_key": key, "artist": artist, "title": title,
               "n_candidates": len(cands)}
        if not cands:
            row.update({"language": None, "status": "no-text",
                        "note": "zero-candidate: only acoustic language ID can answer"})
        else:
            chosen_id = screen_best.get(t["track_id"])
            if chosen_id is None:
                chosen = cands[0]
            else:
                matches = [c for c in cands if c["id"] == chosen_id]
                if len(matches) != 1:
                    raise SystemExit(
                        f"[ERROR] {t['track_id']}: version-screen best {chosen_id!r} matches "
                        f"{len(matches)} candidates -- re-run version-screen")
                chosen = matches[0]
            row["candidate_id"] = chosen["id"]
            row["candidate_source"] = chosen["source"]
            plain = (chosen.get("plain_text") or "").strip()
            if not plain:
                raise SystemExit(f"[ERROR] {t['track_id']}: chosen candidate "
                                 f"{chosen['id']!r} has empty plain_text")
            values = detector.compute_language_confidence_values(plain)
            top = values[0]
            margin = top.value - (values[1].value if len(values) > 1 else 0.0)
            iso3 = top.language.iso_code_639_3.name.lower()
            decided = top.value >= MIN_CONFIDENCE and margin >= MIN_MARGIN
            row.update({
                "language": iso3 if decided else None,
                "status": "decided" if decided else "undecided",
                "detected_iso3": iso3,
                "confidence": round(top.value, 4),
                "margin": round(margin, 4),
                "runners_up": [
                    f"{v.language.iso_code_639_3.name.lower()} {v.value:.3f}" for v in values[1:4]
                ],
            })
        rows.append(row)
        status = row["status"]
        lang = row.get("detected_iso3", "-")
        conf = row.get("confidence")
        print(f"  {t['track_id']:>10s} {status:<10s} {lang:<4s} "
              f"{'' if conf is None else f'{conf:.3f}'}  {artist[:24]} - {title[:36]}")
    out_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"[OK] {out_path} -- {counts} (denominator: {len(rows)} {CORPUS_DIR.name} tracks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
