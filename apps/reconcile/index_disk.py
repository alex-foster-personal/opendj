"""On-disk audio index for link repair -- walk roots, cache, mtime-invalidate.

Entry point::

    python -m apps.reconcile.index_disk                 # build/refresh + summary
    python -m apps.reconcile.index_disk --roots ~/Music # explicit roots
    python -m apps.reconcile.index_disk --rebuild       # ignore the cache

Why a second scanner when :func:`apps.shared.audio_files.scan_music_files`
already exists: that one yields ``(path, size, mtime, ext)`` only and is
re-walked on every call. The candidate matcher in :mod:`apps.reconcile.match`
needs tag-level evidence (duration, ISRC, title, artist), which should not
be recomputed for every unchanged file on every invocation. This module
adds the tag read plus a persistent cache at
``data/state/disk-audio-index.json`` keyed on ``(size, mtime)`` so a rescan
only re-reads tags for files that actually changed.

Read-only: this module never writes, moves, renames or re-encodes an audio
file. The only file it writes is its own JSON cache. It also never *reads the
bytes* of an iCloud-evicted (``SF_DATALESS``) file, because that would fault
gigabytes back down onto the SSD as a side effect of an index build.

Requirements (mini-PRD)
-----------------------
1. Walk configurable roots, default ``~/Music ~/Documents ~/Downloads
   ~/Desktop``, skipping dot-dirs, ``node_modules`` and virtualenvs.  OK
   - [if] a root does not exist [then] it is skipped, not an error.
   - [if] a dir is named ``node_modules`` [then] nothing under it is indexed.
   - [if] a file extension is not in ``paths.AUDIO_EXTENSIONS`` [then] skipped.
2. Record path, basename, size, mtime, duration, ISRC, title, artist.  OK
   - [if] the tag reader cannot parse a file [then] the entry still exists with
     ``tags_read=False`` and ``None`` tag fields, never a crash.
   - [if] a file is an iCloud dataless placeholder [then] it is indexed on
     name+size ONLY and never opened, so no download is triggered.
   - [if] a file carries a TSRC/ISRC tag [then] ``isrc`` is upper-cased and
     stripped of separators.
   - [if] the file has an audio header [then] ``duration_s`` is a float.
3. Cache to ``data/state/disk-audio-index.json`` with mtime invalidation.  OK
   - [if] nothing on disk changed [then] a rebuild re-reads zero tags.
   - [if] a file's size or mtime changed [then] its tags are re-read.
   - [if] a cached file no longer exists [then] its entry is dropped.

Status: OK ran-script works-as-expected, regression tests in
``tests/reconcile/test_link_repair.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from apps.shared import _tagreader, paths
from apps.shared._tagreader import HAS_TAG_READER
from apps.shared.scan_mass_missing import MassMissingError, guard_roots

console = Console(width=120)

# 3: tags now come from tinytag (Thu 1 Oct 2026). A v2 index built without a
# tag reader cached ``tags_read=False`` and empty title/ISRC/duration for
# every entry and reused them by size + mtime, so tag-based relinking stayed
# off until a manual --rebuild. The bump forces one full re-read.
CACHE_VERSION: int = 3
CACHE_PATH: Path = paths.STATE_DIR / "disk-audio-index.json"

# macOS ``SF_DATALESS``: the file is an APFS placeholder whose bytes live in
# iCloud ("Optimise Mac Storage" evicted them). ``stat`` is free but ANY read
# blocks on a network download. A naive tag pass can silently materialise
# evicted files. Index dataless files by name and size without opening them.
SF_DATALESS: int = 0x40000000

DEFAULT_ROOTS: tuple[Path, ...] = (
    paths.HOME / "Music",
    paths.HOME / "Documents",
    paths.HOME / "Downloads",
    paths.HOME / "Desktop",
)

# Directory names never descended into. Dot-dirs are pruned separately by
# prefix so we do not have to enumerate every hidden dir on the machine.
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {"node_modules", ".venv", "venv", "__pycache__", "site-packages", "node-gyp"}
)

# mtime comparison tolerance in seconds. HFS+/APFS round-trips through JSON as
# a float; 1ms is well under any real edit granularity while absorbing float
# repr noise.
MTIME_EPSILON: float = 1e-3


# ----- normalisation (shared with apps.reconcile.match) ------------------

# Supported leading track-number forms: "01 - ", "6. ", "12_", and "01 ".
_LEADING_TRACKNO = re.compile(r"^\s*\d{1,3}(?:\s*[-._)\]]+\s*|\s+)")
# Never let the track-number strip eat the whole title ("7" -> "").
MIN_LEN_AFTER_TRACKNO_STRIP: int = 2
_BRACKETED_NOISE = re.compile(
    r"[\(\[\{][^\)\]\}]*"
    r"(feat|ft|featuring|remix|rmx|edit|mix|version|bootleg|mashup|dub|vip"
    r"|rework|extended|radio|original|instrumental|acapella|clean|dirty)"
    r"[^\)\]\}]*[\)\]\}]",
    re.IGNORECASE,
)
_TRAILING_FEAT = re.compile(r"\s+(feat|ft|featuring)\.?\s+.*$", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^0-9a-z\s]+")
_WS = re.compile(r"\s+")


def strip_accents(text: str) -> str:
    """NFKD-decompose then drop combining marks, then recompose NFC."""
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return unicodedata.normalize("NFC", without_marks)


def normalise_text(text: str | None) -> str:
    """Canonical form used by every fuzzy tier in the matcher.

    Steps, in order (documented because the matcher's thresholds only mean
    something relative to this exact pipeline):

    1. NFKD, drop combining marks, NFC (so ``Beyonce`` == ``Beyonce`` accented).
    2. casefold.
    3. drop a leading track number plus separator (``01 - ``, ``6. ``, ``12_``,
       ``01 ``), unless doing so would leave fewer than
       :data:`MIN_LEN_AFTER_TRACKNO_STRIP` characters.
    4. drop bracketed segments whose contents look like a feat/remix/edit
       qualifier (``(feat. X)``, ``[Extended Mix]``).
    5. drop an unbracketed trailing ``feat. ...`` phrase.
    6. ``&`` becomes ``and``.
    7. drop every remaining non-alphanumeric, non-space character.
    8. collapse runs of whitespace, strip.

    Returns ``""`` for ``None`` / blank input so callers can test truthiness.
    """
    if not text:
        return ""
    out = strip_accents(text).casefold()
    stripped = _LEADING_TRACKNO.sub("", out)
    if len(stripped.strip()) >= MIN_LEN_AFTER_TRACKNO_STRIP:
        out = stripped
    out = _BRACKETED_NOISE.sub(" ", out)
    out = _TRAILING_FEAT.sub("", out)
    out = out.replace("&", " and ")
    out = _NON_ALNUM.sub(" ", out)
    return _WS.sub(" ", out).strip()


def basename_key(name: str) -> str:
    """Case/accent-insensitive basename key (extension retained)."""
    return strip_accents(name).casefold()


def normalise_isrc(raw: str | None) -> str | None:
    """Upper-case ISRC with separators removed, or ``None`` if unusable.

    An ISRC is exactly 12 alphanumerics (CC-XXX-YY-NNNNN). Anything that does
    not reduce to 12 alphanumerics is rejected rather than half-trusted --
    a bogus ISRC would fire the strongest tag tier in the matcher.
    """
    if not raw:
        return None
    compact = re.sub(r"[^0-9A-Za-z]", "", raw).upper()
    return compact if len(compact) == 12 else None


# ----- tag reading -------------------------------------------------------

@dataclass(slots=True)
class TagRead:
    """What the tag reader managed to pull off one file."""

    ok: bool
    duration_s: float | None = None
    title: str | None = None
    artist: str | None = None
    isrc: str | None = None


def read_tags(path: Path) -> TagRead:
    """Read duration/title/artist/ISRC from ``path``.

    Returns ``TagRead(ok=False)`` when the tag reader is absent or the file
    is not parseable as audio. A parse failure is surfaced as data
    (``ok=False``) rather than aborting the entire walk on one corrupt mp3.
    Every other failure mode in this package fails fast.
    """
    if not HAS_TAG_READER:
        return TagRead(ok=False)
    try:
        tag = _tagreader.read(path)
    except _tagreader.TagReadError:
        return TagRead(ok=False)

    return TagRead(
        ok=True,
        duration_s=float(tag.duration) if tag.duration else None,
        title=_clean(tag.title),
        artist=_clean(tag.artist),
        isrc=normalise_isrc(_tagreader.first_other(tag, "isrc")),
    )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ----- index entries -----------------------------------------------------


@dataclass(slots=True)
class DiskAudio:
    """One indexed audio file on this machine."""

    path: str
    size_bytes: int
    mtime: float
    tags_read: bool
    duration_s: float | None = None
    title: str | None = None
    artist: str | None = None
    isrc: str | None = None
    # iCloud placeholder: exists, size is real, bytes are remote. Never opened.
    dataless: bool = False

    @property
    def basename(self) -> str:
        return os.path.basename(self.path)

    @property
    def basename_key(self) -> str:
        return basename_key(self.basename)

    @property
    def stem_key(self) -> str:
        """Normalised filename stem, used as fuzzy fallback text when a file
        carries no usable title tag (a supported missing-tag case)."""
        return normalise_text(os.path.splitext(self.basename)[0])

    @property
    def title_key(self) -> str:
        return normalise_text(self.title)

    @property
    def artist_key(self) -> str:
        return normalise_text(self.artist)


def is_dataless(st: os.stat_result) -> bool:
    """True when ``st`` describes an iCloud-evicted APFS placeholder.

    ``st_flags`` only exists on BSD-family platforms; ``getattr`` keeps this a
    no-op on Linux/Windows rather than a platform branch at every call site.
    """
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS)


def _walk_audio(roots: Iterable[Path]) -> Iterator[tuple[Path, os.stat_result]]:
    """Yield ``(path, stat)`` for every audio file under ``roots``."""
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [
                d
                for d in dirnames
                if not d.startswith(".") and d not in SKIP_DIR_NAMES
            ]
            for name in filenames:
                if name.startswith("."):
                    continue
                if os.path.splitext(name)[1].lower() not in paths.AUDIO_EXTENSIONS:
                    continue
                full = os.path.join(dirpath, name)
                if full in seen:
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    # Broken symlink / permission / vanished mid-walk. Not an
                    # index entry and not fatal.
                    continue
                seen.add(full)
                yield Path(full), st


def _load_cache(cache_path: Path) -> dict[str, DiskAudio]:
    """Read the JSON cache. A missing/unreadable/older-version cache yields
    an empty dict -- the walk then re-reads everything, which is correct if
    slow, never wrong."""
    if not cache_path.exists():
        return {}
    try:
        blob = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(blob, dict) or blob.get("version") != CACHE_VERSION:
        return {}
    out: dict[str, DiskAudio] = {}
    for raw in blob.get("entries", []):
        try:
            out[raw["path"]] = DiskAudio(**raw)
        except (TypeError, KeyError):
            continue
    return out


def _write_cache(cache_path: Path, entries: list[DiskAudio], roots: list[Path]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CACHE_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "roots": [str(r) for r in roots],
        "entries": [asdict(e) for e in entries],
    }
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(cache_path)


@dataclass(slots=True)
class BuildStats:
    """What a build/refresh actually did -- printed, and asserted in tests."""

    walked: int = 0
    reused: int = 0
    tag_reads: int = 0
    dropped: int = 0
    tag_failures: int = 0
    dataless_skipped: int = 0


@dataclass(slots=True)
class DiskIndex:
    """Lookup structures over the indexed files.

    Every dict here is a blocking index: the matcher must never do an O(N)
    scan per broken row; repeatedly comparing every row with every file
    scales as their product.
    """

    entries: list[DiskAudio]
    by_basename: dict[str, list[DiskAudio]] = field(default_factory=dict)
    by_isrc: dict[str, list[DiskAudio]] = field(default_factory=dict)
    by_duration_s: dict[int, list[DiskAudio]] = field(default_factory=dict)
    by_token: dict[str, list[DiskAudio]] = field(default_factory=dict)

    @classmethod
    def build(cls, entries: list[DiskAudio]) -> DiskIndex:
        idx = cls(entries=entries)
        for entry in entries:
            idx.by_basename.setdefault(entry.basename_key, []).append(entry)
            if entry.isrc:
                idx.by_isrc.setdefault(entry.isrc, []).append(entry)
            if entry.duration_s is not None:
                idx.by_duration_s.setdefault(int(entry.duration_s), []).append(entry)
            for token in cls.tokens_for(entry):
                idx.by_token.setdefault(token, []).append(entry)
        return idx

    @staticmethod
    def tokens_for(entry: DiskAudio) -> set[str]:
        """Tokens (len >= 4) from the entry's title tag and filename stem."""
        text = f"{entry.title_key} {entry.artist_key} {entry.stem_key}"
        return {t for t in text.split() if len(t) >= 4}

    def unique_basenames(self) -> int:
        return len(self.by_basename)


def build_index(
    roots: Iterable[Path] | None = None,
    *,
    cache_path: Path = CACHE_PATH,
    rebuild: bool = False,
    write_cache: bool = True,
    allow_mass_missing: bool = False,
) -> tuple[DiskIndex, BuildStats]:
    """Walk ``roots`` and return a ready-to-query index plus build stats.

    Tags are re-read only for paths whose ``(size, mtime)`` differ from the
    cached entry (or that are new). ``rebuild=True`` ignores the cache.
    """
    use_roots = list(roots) if roots is not None else list(DEFAULT_ROOTS)
    prior_cached = _load_cache(cache_path)
    cached = {} if rebuild else prior_cached
    stats = BuildStats()
    entries: list[DiskAudio] = []
    live_paths: set[str] = set()

    for path, st in _walk_audio(use_roots):
        stats.walked += 1
        key = str(path)
        live_paths.add(key)
        prior = cached.get(key)
        if (
            prior is not None
            and prior.size_bytes == st.st_size
            and abs(prior.mtime - st.st_mtime) <= MTIME_EPSILON
        ):
            stats.reused += 1
            entries.append(prior)
            continue
        if is_dataless(st):
            # Do NOT open: that would fault the bytes down from iCloud.
            stats.dataless_skipped += 1
            entries.append(
                DiskAudio(
                    path=key,
                    size_bytes=st.st_size,
                    mtime=st.st_mtime,
                    tags_read=False,
                    dataless=True,
                )
            )
            continue
        tag = read_tags(path)
        stats.tag_reads += 1
        if not tag.ok:
            stats.tag_failures += 1
        entries.append(
            DiskAudio(
                path=key,
                size_bytes=st.st_size,
                mtime=st.st_mtime,
                tags_read=tag.ok,
                duration_s=tag.duration_s,
                title=tag.title,
                artist=tag.artist,
                isrc=tag.isrc,
            )
        )

    stats.dropped = len(set(cached) - live_paths)
    guard_roots(
        use_roots,
        live_paths,
        prior_cached,
        allow_mass_missing=allow_mass_missing,
    )
    if write_cache:
        _write_cache(cache_path, entries, use_roots)
    return DiskIndex.build(entries), stats


# ----- CLI ---------------------------------------------------------------


def _print_summary(index: DiskIndex, stats: BuildStats, cache_path: Path) -> None:
    table = Table(title="Disk audio index", show_lines=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Audio files indexed", f"{stats.walked}")
    table.add_row("  reused from cache", f"{stats.reused}")
    table.add_row("  tags read this run", f"{stats.tag_reads}")
    table.add_row("  tag reads that failed", f"{stats.tag_failures}")
    table.add_row(
        "  iCloud dataless (never opened)", f"[yellow]{stats.dataless_skipped}[/yellow]"
    )
    table.add_row("  stale entries dropped", f"{stats.dropped}")
    table.add_row("Distinct basenames", f"{index.unique_basenames()}")
    ambiguous = sum(1 for v in index.by_basename.values() if len(v) > 1)
    table.add_row("  basenames with 2+ files", f"{ambiguous}")
    table.add_row("Files with ISRC tag", f"{sum(1 for e in index.entries if e.isrc)}")
    table.add_row(
        "Files with duration", f"{sum(1 for e in index.entries if e.duration_s)}"
    )
    table.add_row("Cache", str(cache_path))
    console.print(table)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.reconcile.index_disk",
        description="Index audio files on disk for link repair (read-only).",
    )
    p.add_argument(
        "--roots",
        nargs="+",
        type=Path,
        default=None,
        help=f"roots to walk (default: {' '.join(str(r) for r in DEFAULT_ROOTS)})",
    )
    p.add_argument(
        "--cache", type=Path, default=CACHE_PATH, help="index cache json path"
    )
    p.add_argument(
        "--rebuild", action="store_true", help="ignore the cache and re-read all tags"
    )
    p.add_argument(
        "--allow-mass-missing",
        action="store_true",
        help="override LIBM-41: allow a scan that drops more than 50% of a "
        "previously populated root (including to zero)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not HAS_TAG_READER:
        console.print(
            "[yellow]tinytag not importable: duration/ISRC/title/artist will be "
            "empty and the tag-based matcher tiers cannot fire. "
            "Reinstall the environment (`uv sync`).[/yellow]"
        )
    roots = args.roots if args.roots else list(DEFAULT_ROOTS)
    console.print(f"[bold]Walking:[/bold] {', '.join(str(r) for r in roots)}")
    started = time.monotonic()
    try:
        index, stats = build_index(
            roots,
            cache_path=args.cache,
            rebuild=args.rebuild,
            allow_mass_missing=args.allow_mass_missing,
        )
    except MassMissingError as exc:
        console.print(f"[red]error:[/red] {exc}")
        return 1
    console.print(f"[bold]Done[/bold] in {time.monotonic() - started:.1f}s")
    _print_summary(index, stats, args.cache)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
