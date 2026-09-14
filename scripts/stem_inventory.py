#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3>=1.34"]
# ///
"""Inventory every stem bundle on this machine, wherever it drifted to.

WHY THIS EXISTS. Stem bundles were produced by four different efforts (the
RoFormer library spike, an A/B render, a review-clip batch, and a bought-in
acapella pack) and each landed in its own directory with its own naming. Only
one of those stores is a shape ``apps.stems.artifacts`` can load,
so bundles that cost GPU hours are sitting on disk invisible to the app. You
cannot centralize what you have not counted, so counting is step one.

TWO BUNDLE FORMS, and the reason both must be understood:

  * DIRECTORY form -- ``<root>/<stable_id>/vocals.mp3`` plus a manifest. This
    is the only form the loader accepts.
  * LOOSE form -- ``<root>/<title>-vocals.mp3`` as flat siblings. The stems
    are perfectly good; the identity is in ``meta.json``'s ``source_path``
    rather than in the directory name, so the loader cannot see them at all.

Loose bundles are therefore reported with the stable_id they RESOLVE to, via
the source path recorded at render time joined against the state database.
That join is what turns a pile of title-named mp3s back into library coverage.

HONEST DENOMINATOR (docs/library-availability.md). Coverage is quoted against
tracks whose audio actually resolves -- ``present``, or ``present`` plus
``awaiting_volume`` -- never against every row in ``tracks``. The denominator
is re-measured on every run and printed beside the figure, because it moves.

NOTHING HERE WRITES OR DELETES. This module only reads, and it is imported by
``scripts/local_stems_to_r2.py`` for the migration's discovery half, so the
two can never disagree about what a bundle is.

Requirements (mini-PRD):
  ✔︎ ✅ every configured root is scanned in both bundle forms.
    [if] a root holds loose title-named stems [then] they are found and counted
    [if] a root does not exist [then] it is reported as missing, not skipped
  ✔︎ ✅ loose bundles resolve to a stable_id through meta.json's source_path.
    [if] a source path matches no track row [then] it is flagged unresolved
  ✔︎ ✅ coverage is quoted against a re-measured availability denominator.
    [if] the denominator is quoted from memory [then ⛔️] the figure is void
  ✔︎ ✅ duplicate stable_ids across roots are reported, never resolved silently.
    [if] two roots hold the same id [then] both paths are printed for a human
  ✔︎ ✅ R2 presence is a real listing or an explicit refusal, never a guess.
    [if] --check-r2 runs without credentials [then ⛔️] exit non-zero, say why

MDT_EXTERNAL_STEM_ROOTS names this machine's external stem store path(s),
os.pathsep-separated (e.g. "/Users/you/Music/_incoming/pack/stems"). Required
when the default roots are used (no --root override): a real machine path
must never be hardcoded in tracked code (#910), and the script fails fast
if it is unset rather than silently scanning zero external roots.

Run:
  MDT_EXTERNAL_STEM_ROOTS=/Users/you/Music/_incoming/clubsauna-acapella-techno-100/stems \
    uv run scripts/stem_inventory.py
  uv run scripts/stem_inventory.py --json out.json
  doppler run --project general --config dev_personal -- \
    uv run scripts/stem_inventory.py --check-r2

-Claude
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.r2_stems import (
    DEFAULT_BUCKET,
    R2_STEM_PREFIX,
    list_r2_sizes,
    r2_client,
)
from scripts.stem_inventory_report import (
    build_report,
    print_report,
)

REPO_ROOT: Path = Path(__file__).resolve().parent.parent

LAYOUT_PARTS: dict[str, frozenset[str]] = {
    "roformer2": frozenset({"vocals", "instrumental"}),
    "demucs4": frozenset({"bass", "drums", "other", "vocals"}),
}
AUDIO_SUFFIXES: frozenset[str] = frozenset({".mp3", ".flac", ".wav", ".opus"})
MANIFEST: str = "manifest.json"
META: str = "meta.json"
UNKNOWN_PRESET: str = "unknown"

# A stem part name optionally preceded by the separator the various renderers
# used: "title-vocals.mp3" from the spike, "Artist - Title - vocals.mp3" from
# the acapella pack. Anchored at the end so a track called "Drums" is not
# mistaken for a drums stem.
_LOOSE_RE = re.compile(
    r"^(?P<slug>.+?)\s*-\s*(?P<part>vocals|instrumental|bass|drums|other)$",
    re.IGNORECASE,
)
_STABLE_ID_RE = re.compile(r"^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$")

# Stores that exist outside any data directory. The acapella pack was bought
# in and unpacked next to the audio it came with, so it never had a reason to
# live under data/, which is precisely why it went uncounted for so long.
# There is no portable default (#910): the location is wherever THIS machine
# unpacked the pack, so it comes from MDT_EXTERNAL_STEM_ROOTS, os.pathsep-
# separated absolute paths, e.g.
# "/Users/you/Music/_incoming/clubsauna-acapella-techno-100/stems". Unset is
# a hard error rather than an empty tuple: an empty tuple would make the
# inventory silently stop counting this store's bundles and still print a
# healthy-looking report, which is exactly the failure #910 introduced.
EXTERNAL_ROOTS_ENV: str = "MDT_EXTERNAL_STEM_ROOTS"


def _external_roots() -> tuple[Path, ...]:
    """External stem stores from ``MDT_EXTERNAL_STEM_ROOTS``, fail-fast.

    Read fresh (not cached at import time) so a test can set/unset the
    variable per case. Every configured entry must exist by the time this
    returns: an operator who names a root here is affirmatively claiming it
    is there, unlike the in-repo stores in :func:`default_roots`, where "not
    here yet" is a normal, reportable state.
    """
    raw = os.environ.get(EXTERNAL_ROOTS_ENV)
    if not raw:
        raise RuntimeError(
            f"{EXTERNAL_ROOTS_ENV} is not set -- export this machine's "
            "external stem store path(s), os.pathsep-separated (e.g. "
            f"{EXTERNAL_ROOTS_ENV}=/Users/you/Music/_incoming/"
            "clubsauna-acapella-techno-100/stems); there is no safe default"
        )
    roots = tuple(Path(p) for p in raw.split(os.pathsep) if p)
    for root in roots:
        if not root.is_dir():
            raise FileNotFoundError(
                f"{EXTERNAL_ROOTS_ENV} entry does not exist: {root}"
            )
    return roots


def default_roots(data_dir: Path) -> tuple[Path, ...]:
    """Every store known to hold renders, relative to one data directory.

    Review and bench directories are included deliberately: they hold real
    renders, and the point of the exercise is to find what drifted, not to
    re-confirm the one store we already knew about.

    The scratch root hangs off the data directory's OWNING checkout rather
    than this file's, so a worktree pointed at the primary data directory
    inventories the primary checkout's scratch too. Deriving it from
    ``__file__`` instead would silently report an empty worktree scratch as
    the truth, which is the class of error this whole script exists to end.
    """
    return (
        data_dir / "state/stems",
        data_dir / "state/stems-roformer-spike",
        data_dir / "state/stems-roformer-spike-verify",
        data_dir / "state/stems-demucs-ab",
        data_dir.parent / ".tmp/.tmp_pleasure_stem_review",
        *_external_roots(),
    )


#----- model ------------------------------------------------------------------

@dataclass
class Bundle:
    """One track's stems in one store, in whichever form they landed."""

    root: Path
    form: str                              # "directory" | "loose"
    slug: str                              # dir name, or the loose filename stem
    layout: str                            # roformer2 | demucs4 | unknown
    preset: str                            # R2 key segment; UNKNOWN_PRESET if unproven
    files: dict[str, Path] = field(default_factory=dict)   # dest name -> source
    stable_id: str | None = None
    source_path: str | None = None
    # Every id the source path could belong to. Length > 1 means the library
    # holds several rows for one file, so the owner cannot be decided here.
    candidate_ids: tuple[str, ...] = ()
    issues: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.files.values() if p.is_file())

    @property
    def parts(self) -> set[str]:
        return {name.rsplit(".", 1)[0] for name in self.files if name != MANIFEST}

    @property
    def is_complete(self) -> bool:
        """Every audio part its layout requires. Extension-agnostic, because
        the same layout was rendered to mp3, flac and wav at different times."""
        required = LAYOUT_PARTS.get(self.layout)
        if required is None:
            return False
        return required.issubset(self.parts)

    @property
    def has_manifest(self) -> bool:
        return MANIFEST in self.files

    @property
    def is_publishable(self) -> bool:
        """Safe to put in R2: complete, identified, presettable, and loadable.

        Each clause rules out a bundle that would be worse in R2 than on disk:

        * an unproven preset guesses the middle segment of
          ``stems/<preset>/<stable_id>/<file>``, filing the bundle where no
          consumer will look for it;
        * a missing manifest.json is rejected outright by
          ``stem_artifacts.load_stem_bundle``, so publishing one ships an
          object that every reader fails on. Uploading it would convert a
          visible local gap into an invisible remote one.

        Bundles that clear every clause but the manifest are counted
        separately as ``needs_manifest`` rather than quietly dropped: they are
        real renders, and synthesizing a manifest for them is follow-on work
        someone has to be able to see.
        """
        return (
            self.is_complete
            and self.stable_id is not None
            and self.preset != UNKNOWN_PRESET
            and self.has_manifest
        )

    @property
    def needs_manifest(self) -> bool:
        """Recoverable, but blocked on a manifest it never had written."""
        return (
            self.is_complete
            and self.stable_id is not None
            and self.preset != UNKNOWN_PRESET
            and not self.has_manifest
        )

    def key_for(self, filename: str) -> str:
        if self.stable_id is None:
            raise ValueError(f"{self.slug!r} has no stable_id, so it has no R2 key")
        if self.preset == UNKNOWN_PRESET:
            raise ValueError(f"{self.slug!r} has no proven preset, so it has no R2 key")
        return f"{R2_STEM_PREFIX}/{self.preset}/{self.stable_id}/{filename}"


#----- identity ---------------------------------------------------------------

class TrackResolver:
    """Maps a render's recorded source path back to a library stable_id.

    Both ``tracks.file_path`` and ``track_locations.file_path`` are consulted:
    a track that has since moved keeps its old path as an alternate location,
    and that alternate is often exactly the path a months-old render recorded.

    ONE PATH CAN CARRY SEVERAL IDS. 785 paths in this library resolve to more
    than one stable_id, because a re-import mints a fresh id while the old row
    keeps the same file_path. An earlier draft of this class kept a
    ``dict[path, id]`` and let one of them win by insertion order, which
    silently attributed a bundle to whichever row happened to be read first
    and made the answer depend on the query plan. Candidates are therefore
    kept as a set and a single id is returned ONLY when there is exactly one:
    an ambiguous path is a finding for a human, not a coin flip.
    """

    def __init__(self, db_path: Path) -> None:
        if not db_path.is_file():
            raise FileNotFoundError(
                f"state database not found at {db_path}. Pass --db, or run from "
                "a checkout whose data/ directory is populated."
            )
        self._by_path: dict[str, set[str]] = defaultdict(set)
        self.availability: dict[str, str] = {}
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for table in ("tracks", "track_locations"):
                for path, stable_id in connection.execute(
                    f"SELECT file_path, stable_id FROM {table} "
                    "WHERE file_path IS NOT NULL AND file_path != ''"
                ):
                    self._by_path[path].add(stable_id)
            self.availability = dict(
                connection.execute(
                    "SELECT stable_id, state FROM track_availability"
                )
            )
        finally:
            connection.close()

    def candidates_for_path(self, source_path: str | None) -> tuple[str, ...]:
        """Every stable_id this path could belong to, sorted for stable output."""
        if not source_path:
            return ()
        return tuple(sorted(self._by_path.get(source_path, ())))

    def stable_id_for_path(self, source_path: str | None) -> str | None:
        """The id, but only when the path names exactly one track."""
        candidates = self.candidates_for_path(source_path)
        return candidates[0] if len(candidates) == 1 else None

    def denominator(self) -> dict[str, int]:
        """Availability buckets, re-measured. Never cite a remembered figure."""
        return dict(Counter(self.availability.values()))


def looks_like_stable_id(value: str) -> bool:
    """A 40-hex sha1 or a 64-hex sha256 directory name."""
    return bool(_STABLE_ID_RE.fullmatch(value))


#----- scanning ---------------------------------------------------------------

def _read_json(path: Path) -> dict[str, Any]:
    """Malformed sidecars are a finding, not a crash: return {} and let the
    caller record the issue against the bundle it belongs to."""
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _layout_and_preset(
    manifest: dict[str, Any], meta: dict[str, Any], parts: set[str]
) -> tuple[str, str]:
    """Prefer what the renderer declared; fall back to what the files prove.

    The preset is the R2 key's middle segment, so it is taken only from a
    recorded tag. Inferring it from the part names would produce a plausible
    string that no farm run ever used.
    """
    layout = str(manifest.get("layout") or "").strip()
    if not layout:
        for name, required in LAYOUT_PARTS.items():
            if required.issubset(parts):
                layout = name
                break
    if not layout:
        layout = "unknown"

    model = manifest.get("model")
    preset = ""
    if isinstance(model, dict):
        preset = str(model.get("version") or "").strip()
    if not preset:
        preset = str(meta.get("config_tag") or "").strip()
    return layout, preset or UNKNOWN_PRESET


def scan_directory_bundles(root: Path) -> list[Bundle]:
    """Bundles stored the way the loader expects: one directory per stable_id."""
    bundles: list[Bundle] = []
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        files = {
            item.name: item
            for item in sorted(child.iterdir())
            if item.is_file() and item.suffix.lower() in AUDIO_SUFFIXES
        }
        if not files:
            continue
        manifest_path = child / MANIFEST
        manifest = _read_json(manifest_path) if manifest_path.is_file() else {}
        meta_path = child / META
        meta = _read_json(meta_path) if meta_path.is_file() else {}
        parts = {name.rsplit(".", 1)[0] for name in files}
        layout, preset = _layout_and_preset(manifest, meta, parts)

        bundle = Bundle(
            root=root,
            form="directory",
            slug=child.name,
            layout=layout,
            preset=preset,
            files=files,
            source_path=str(meta.get("source_path") or "") or None,
        )
        if manifest_path.is_file():
            bundle.files[MANIFEST] = manifest_path
        else:
            bundle.issues.append("no manifest.json (loader would reject it)")
        if looks_like_stable_id(child.name):
            bundle.stable_id = child.name.lower()
        else:
            bundle.issues.append("directory name is not a stable id")
        if preset == UNKNOWN_PRESET:
            bundle.issues.append("no recorded preset tag")
        bundles.append(bundle)
    return bundles


def scan_loose_bundles(root: Path, resolver: TrackResolver | None) -> list[Bundle]:
    """Bundles that landed as flat title-named siblings instead of directories.

    Grouped by the slug left when the trailing part name is stripped. The
    sidecar is matched by exact slug first; the acapella pack names its audio
    after the track but its meta after an index-plus-Spotify-id, so a leading
    numeric index is the documented second attempt. Anything still unmatched
    is reported rather than guessed at.
    """
    grouped: dict[str, dict[str, Path]] = defaultdict(dict)
    for item in sorted(root.iterdir()):
        if not item.is_file() or item.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        matched = _LOOSE_RE.match(item.stem)
        if matched is None:
            continue
        slug = matched.group("slug").strip()
        grouped[slug][f"{matched.group('part').lower()}{item.suffix.lower()}"] = item

    sidecars = {p.stem: p for p in root.glob("*-meta.json")}
    by_index: dict[str, Path] = {}
    for stem, path in sidecars.items():
        index = stem.split("_", 1)[0].split("-", 1)[0].strip()
        if index.isdigit():
            by_index.setdefault(index, path)

    bundles: list[Bundle] = []
    for slug, files in sorted(grouped.items()):
        meta_path = sidecars.get(f"{slug}-meta")
        if meta_path is None:
            leading = slug.split(" ", 1)[0].split("-", 1)[0].strip()
            meta_path = by_index.get(leading) if leading.isdigit() else None
        meta = _read_json(meta_path) if meta_path is not None else {}
        parts = {name.rsplit(".", 1)[0] for name in files}
        layout, preset = _layout_and_preset({}, meta, parts)
        source_path = str(meta.get("source_path") or "") or None

        bundle = Bundle(
            root=root,
            form="loose",
            slug=slug,
            layout=layout,
            preset=preset,
            files=dict(files),
            source_path=source_path,
        )
        if meta_path is None:
            bundle.issues.append("no meta.json sidecar, so no source path")
        elif source_path is None:
            bundle.issues.append("meta.json records no source_path")
        _attach_identity(bundle, resolver)
        bundle.issues.append("loose form: the stem loader cannot see this bundle")
        bundles.append(bundle)
    return bundles


def _attach_identity(bundle: Bundle, resolver: TrackResolver | None) -> None:
    """Resolve a loose bundle's owner, recording WHY when it cannot be decided.

    Three outcomes, kept distinct because they need different follow-up: one
    candidate is a clean resolution, several means the library holds more than
    one row for that file and a human must pick, and none means the audio was
    never in the library at all (the bought-in acapella pack).
    """
    if resolver is None:
        return
    bundle.candidate_ids = resolver.candidates_for_path(bundle.source_path)
    bundle.stable_id = resolver.stable_id_for_path(bundle.source_path)
    if bundle.stable_id is not None or bundle.source_path is None:
        return
    if len(bundle.candidate_ids) > 1:
        # Publishing under a guessed id files the stems against the wrong
        # track, which is harder to notice than not publishing at all.
        bundle.issues.append(
            f"source path maps to {len(bundle.candidate_ids)} stable_ids, "
            "so the owner is ambiguous"
        )
    else:
        bundle.issues.append("source path matches no track row")


def scan_root(root: Path, resolver: TrackResolver | None) -> list[Bundle]:
    """Both forms in one store. A missing root raises: a silently skipped
    store is exactly how bundles went missing in the first place."""
    if not root.is_dir():
        raise FileNotFoundError(f"stem root does not exist: {root}")
    return scan_directory_bundles(root) + scan_loose_bundles(root, resolver)


def scan_roots(
    roots: Iterable[Path], resolver: TrackResolver | None
) -> tuple[list[Bundle], list[Path], list[str]]:
    """Scan every root, collecting missing ones rather than dying on the first.

    A store that has been cleaned up is a normal state and should not stop the
    inventory of the others, but it must still appear in the report. Returns
    the bundles, the roots that were actually scanned, and the ones that were
    not there.
    """
    found: list[Bundle] = []
    scanned: list[Path] = []
    missing: list[str] = []
    for root in roots:
        try:
            found.extend(scan_root(root, resolver))
        except FileNotFoundError:
            missing.append(str(root))
        else:
            scanned.append(root)
    return found, scanned, missing


#----- cli --------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uv run scripts/stem_inventory.py",
        description="Inventory every stem bundle on this machine",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=REPO_ROOT / "data",
        help="data directory holding state/. Worktrees point this at the "
             "primary checkout, which is where the stems actually live.",
    )
    parser.add_argument(
        "--root", action="append", type=Path, default=None,
        help="stem store to scan; repeatable. Defaults to the known stores.",
    )
    parser.add_argument(
        "--db", type=Path, default=None,
        help="state database; defaults to <data-dir>/state/state.db",
    )
    parser.add_argument(
        "--json", type=Path, default=None,
        help="also write the full report, including every duplicate, here",
    )
    parser.add_argument(
        "--check-r2", action="store_true",
        help="list the bucket and report which bundles are already published. "
             "Needs R2 credentials and fails loudly without them.",
    )
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    roots = tuple(args.root) if args.root else default_roots(args.data_dir)
    db_path = args.db if args.db is not None else args.data_dir / "state/state.db"

    resolver = TrackResolver(db_path) if db_path.is_file() else None
    if resolver is None:
        print(
            f"warning: no state database at {db_path}; loose bundles cannot be "
            "resolved to stable_ids and no coverage figure will be quoted.",
            file=sys.stderr,
        )

    bundles, scanned, missing = scan_roots(roots, resolver)
    remote = list_r2_sizes(r2_client(), args.bucket) if args.check_r2 else None

    report = build_report(bundles, scanned, missing, resolver, remote)
    print_report(report, bundles)

    if args.json is not None:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
