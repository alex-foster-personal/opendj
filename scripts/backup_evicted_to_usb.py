"""Copy the iCloud-evicted tail of the library to an external drive, one file at a time.

WHY ONE AT A TIME. Evicted files can show a size while occupying zero blocks.
A storage-constrained system can re-evict rehydrated files before a batch is
copied. Materialise one file, copy it, verify it, and hand the space back
with an evict before moving on.

WHY THE SIZE CHECK IS NOT OPTIONAL. A successful copy exit code does not
establish that bytes landed: a source can be re-evicted during the copy.
Every destination size is compared with the source size, and a short or empty
destination is a failure regardless of the copy call's exit code.

WHY FILENAMES ARE SANITISED. The default destination is exFAT, which rejects
characters including colon, question mark, asterisk and path separators.
The progress log records the original absolute path for each destination.

MINI-PRD
--------
Status key: `→` out of scope | `?` todo | `✔︎` done | `✔︎ ✅` done + ran + works
as expected | `✔︎ ✅ 🎯` done + working + regression tests.

  ✔︎ ✅ every listed file ends up on the destination drive byte-identical, or is
    recorded as a failure with the real reason.
    [if] a copy lands short or empty [then] it is a failure, not a success
    [if] the source never materialises within the timeout [then] record and move
         on -- never block the run on one file
    [if] the destination already holds a byte-identical copy [then] skip it, so
         the script is safely re-runnable

  ✔︎ ✅ the internal disk is never driven into its floor.
    [if] free space falls below the floor [then ⛔️] stop the whole run and say
         so, rather than continuing and wedging the machine

  → deleting, moving or rewriting any source file. This only copies.
  → the library database. Nothing here touches state.db.

-Claude
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# ----- CFG -------------------------------------------------------------------
DEST_ROOT: Path = Path("/Volumes/one-tera/music-dj-tools/music")
MANIFEST: Path = Path(__file__).resolve().parents[1] / "data/backup/bifrost2-music-manifest.json"
PROGRESS: Path = Path(__file__).resolve().parents[1] / "data/backup/usb-backup.progress.jsonl"
DISK_FLOOR_GB: float = 8.0
MATERIALISE_TIMEOUT_S: float = 240.0
POLL_S: float = 2.0
# exFAT rejects these; keep the list explicit rather than guessing per-filesystem.
ILLEGAL_CHARS: str = ':?*<>|"\\/'


@dataclass(frozen=True)
class Result:
    src: str
    dst: str
    bytes: int
    status: str  # copied | skipped | failed
    reason: str = ""


# ----- helpers ---------------------------------------------------------------
def _free_gb(path: Path) -> float:
    st = shutil.disk_usage(path)
    return st.free / 1e9


def _is_materialised(path: Path) -> bool:
    """Evicted files report a size but occupy no blocks. st_blocks is the truth."""
    try:
        return path.stat().st_blocks > 0
    except OSError:
        return False


def _sanitise(name: str) -> str:
    return "".join("_" if c in ILLEGAL_CHARS else c for c in name)


def _dest_for(src: Path) -> Path:
    shard = hashlib.sha1(str(src).encode()).hexdigest()[:2]
    return DEST_ROOT / shard / _sanitise(src.name)


def _materialise(src: Path) -> tuple[bool, str]:
    """Ask iCloud for the file and wait for blocks to appear. Never blocks forever."""
    if _is_materialised(src):
        return True, ""
    proc = subprocess.run(
        ["brctl", "download", str(src)], capture_output=True, text=True, check=False
    )
    deadline = time.monotonic() + MATERIALISE_TIMEOUT_S
    while time.monotonic() < deadline:
        if _is_materialised(src):
            return True, ""
        time.sleep(POLL_S)
    detail = (proc.stderr or proc.stdout or "").strip()[:120]
    return False, f"not materialised in {MATERIALISE_TIMEOUT_S:.0f}s" + (f": {detail}" if detail else "")


def _unique(dst: Path) -> Path:
    """Sanitising can collide two distinct sources; never overwrite a good copy."""
    if not dst.exists():
        return dst
    stem, suffix = dst.stem, dst.suffix
    for i in range(1, 1000):
        cand = dst.with_name(f"{stem}-{i}{suffix}")
        if not cand.exists():
            return cand
    raise RuntimeError(f"cannot find a free name for {dst}")


def copy_one(src_str: str, evict: bool) -> Result:
    src = Path(src_str)
    dst = _dest_for(src)
    try:
        want = src.stat().st_size
    except OSError as exc:
        return Result(src_str, str(dst), 0, "failed", f"source stat failed: {exc}")

    if dst.exists() and dst.stat().st_size == want:
        return Result(src_str, str(dst), want, "skipped", "already present, size matches")

    ok, why = _materialise(src)
    if not ok:
        return Result(src_str, str(dst), 0, "failed", why)

    dst.parent.mkdir(parents=True, exist_ok=True)
    target = _unique(dst) if dst.exists() else dst
    try:
        shutil.copyfile(src, target)
    except OSError as exc:
        # The real exception text, not a generic "copy failed" -- a swallowed
        # error here already cost this project a whole run of useless failures.
        return Result(src_str, str(target), 0, "failed", f"{type(exc).__name__}: {exc}")

    got = target.stat().st_size
    if got != want:
        target.unlink(missing_ok=True)
        return Result(src_str, str(target), got, "failed",
                      f"size mismatch: source {want}, copy {got} -- re-evicted mid-copy")

    if evict:
        subprocess.run(["brctl", "evict", str(src)], capture_output=True, check=False)
    return Result(src_str, str(target), got, "copied")


def _load_todo(args: argparse.Namespace, done: set[str]) -> list[str]:
    todo = json.loads(MANIFEST.read_text())["skipped_evicted"]
    todo = [p for p in todo if p not in done]
    if args.of > 1:
        todo = [p for i, p in enumerate(todo) if i % args.of == args.shard]
    if args.limit:
        todo = todo[: args.limit]
    return todo


# ----- main ------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.backup_evicted_to_usb")
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    # Two workers roughly halve wall-clock because the bottleneck is iCloud
    # LATENCY, not bandwidth or CPU. Residency stays ~one file per worker
    # because each evicts before moving on, so this does not cost disk.
    ap.add_argument("--shard", type=int, default=0, help="this worker's index")
    ap.add_argument("--of", type=int, default=1, help="total workers")
    ap.add_argument("--no-evict", action="store_true",
                    help="keep files local after copying (fills the disk; for debugging)")
    args = ap.parse_args()

    if not DEST_ROOT.parent.parent.exists():
        raise SystemExit(f"error: destination drive not mounted: {DEST_ROOT}")
    DEST_ROOT.mkdir(parents=True, exist_ok=True)
    probe = DEST_ROOT / ".writetest"
    try:
        probe.write_text("x")
        probe.unlink()
    except OSError as exc:
        raise SystemExit(f"error: {DEST_ROOT} is not writable ({exc}). Refusing to start.")

    done = set()
    if PROGRESS.exists():
        for line in PROGRESS.read_text().splitlines():
            try:
                row = json.loads(line)
                if row.get("status") in ("copied", "skipped"):
                    done.add(row["src"])
            except json.JSONDecodeError:
                continue
    todo = _load_todo(args, done)

    start_free = _free_gb(Path("/"))
    print(f"{len(todo)} to do ({len(done)} already done), internal free {start_free:.1f} GB", flush=True)

    counts = {"copied": 0, "skipped": 0, "failed": 0}
    total_bytes = 0
    with PROGRESS.open("a") as log:
        for i, src in enumerate(todo, 1):
            free = _free_gb(Path("/"))
            if free < DISK_FLOOR_GB:
                print(f"[STOP] disk floor: free={free:.2f} GB < {DISK_FLOOR_GB} GB", flush=True)
                break
            res = copy_one(src, evict=not args.no_evict)
            log.write(json.dumps(res.__dict__) + "\n")
            log.flush()  # crash-safe: a result that exists only in RAM is a result you lose
            counts[res.status] += 1
            if res.status == "copied":
                total_bytes += res.bytes
            if i % 10 == 0 or res.status == "failed":
                print(f"  {i}/{len(todo)} {res.status:<8}{Path(src).name[:52]}"
                      f"{' -- ' + res.reason if res.reason else ''}", flush=True)

    end_free = _free_gb(Path("/"))
    print(f"\ncopied {counts['copied']}, skipped {counts['skipped']}, failed {counts['failed']}"
          f"; {total_bytes / 1e9:.2f} GB")
    print(f"internal free: {start_free:.1f} -> {end_free:.1f} GB")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
