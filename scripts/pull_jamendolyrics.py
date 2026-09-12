# /// script
# requires-python = ">=3.11"
# dependencies = ["huggingface_hub>=0.30"]
# ///
"""Pull the JamendoLyrics MultiLang dataset (word-level lyric alignment ground truth).

Requirements
- ✔︎ Download canonical dataset from HF `jamendolyrics/jamendolyrics` (the GitHub
  f90/jamendolyrics repo is DEPRECATED as of Wed 30 Apr 2025).
    [if] run completes [then] data/datasets/jamendolyrics holds mp3/, annotations/, lyrics/
    [if] HF unreachable [then ⛔️] non-zero exit with the underlying error, no partial "ok"
- ✔︎ The repo stores real audio under `subsets/<lang>/mp3/`; `mp3/*.mp3` are git symlinks,
  which snapshot_download materializes as tiny text files holding the relative target.
  Convert those back into real symlinks so `mp3/` is usable directly.
    [if] any mp3/*.mp3 is a pointer text file after the run [then ⛔️] exit 1
- ✔︎ Fail-fast verification: 79 songs, every mp3/ entry resolves to real MPEG audio.
    [if] mp3 count != 79 or words-CSV count != 79 [then ⛔️] exit 1 with counts named
    [if] a resolved mp3 is smaller than 100 KB [then ⛔️] exit 1 naming the file

Usage: uv run scripts/pull_jamendolyrics.py [--dest DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

REPO_ID = "jamendolyrics/jamendolyrics"
DEFAULT_DEST = Path(__file__).resolve().parents[1] / "data" / "datasets" / "jamendolyrics"
IGNORE_PATTERNS = ["plots/*"]
EXPECTED_SONGS = 79
MIN_AUDIO_BYTES = 100_000

# -----------------------------------------------------------------------------


def _restore_mp3_symlinks(dest: Path) -> None:
    """Turn materialized git-symlink pointer files in mp3/ back into real symlinks."""
    for mp3 in sorted((dest / "mp3").glob("*.mp3")):
        if mp3.is_symlink() or mp3.stat().st_size > 1024:
            continue
        target = mp3.read_text(encoding="utf-8").strip()
        if not (mp3.parent / target).resolve().is_file():
            raise FileNotFoundError(f"{mp3.name}: pointer target missing: {target}")
        mp3.unlink()
        mp3.symlink_to(target)
        print(f"[OK] symlinked mp3/{mp3.name} -> {target}")


def _verify(dest: Path) -> list[str]:
    failures: list[str] = []
    counts = {
        "mp3/*.mp3": len(list((dest / "mp3").glob("*.mp3"))),
        "annotations/words/*.csv": len(list((dest / "annotations" / "words").glob("*.csv"))),
        "annotations/lines/*.csv": len(list((dest / "annotations" / "lines").glob("*.csv"))),
        "lyrics/*.words.txt": len(list((dest / "lyrics").glob("*.words.txt"))),
    }
    for pattern, n in counts.items():
        if n != EXPECTED_SONGS:
            failures.append(f"{pattern}: expected {EXPECTED_SONGS}, found {n}")
        else:
            print(f"[OK] {pattern}: {n}")
    if not (dest / "JamendoLyrics.csv").is_file():
        failures.append("JamendoLyrics.csv missing")
    else:
        print("[OK] JamendoLyrics.csv present")
    small = [p.name for p in (dest / "mp3").glob("*.mp3") if p.resolve().stat().st_size < MIN_AUDIO_BYTES]
    if small:
        failures.append(f"mp3 entries not resolving to real audio (<{MIN_AUDIO_BYTES} B): {small[:5]}")
    else:
        print("[OK] all mp3/ entries resolve to real audio")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    args = parser.parse_args()

    args.dest.mkdir(parents=True, exist_ok=True)
    print(f"[..] snapshot_download {REPO_ID} -> {args.dest}")
    snapshot_download(
        repo_id=REPO_ID,
        repo_type="dataset",
        local_dir=args.dest,
        ignore_patterns=IGNORE_PATTERNS,
    )
    _restore_mp3_symlinks(args.dest)

    failures = _verify(args.dest)
    if failures:
        for f in failures:
            print(f"[ERROR] {f}", file=sys.stderr)
        return 1
    print(f"[OK] JamendoLyrics complete at {args.dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
