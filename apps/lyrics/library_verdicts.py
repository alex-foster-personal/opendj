"""Library-scale ``lyric_verdict`` backfill from existing stem bundles.

WHY (measured Mon 14 Sep 2026): 915 of 921 present tracks already carry a
4-stem demucs bundle (mostly MP3 parts, schema v1) under
``data/state/stems/<stable_id>/``, but ``lyric_verdict`` has zero rows for
them -- the library browser's lyrics column reads every one as "not
processed". The verdict those tracks need (vocal / sparse / no-lyrics) is a
pure function of stem vocal coverage (:mod:`apps.lyrics.vocal_presence`), so
it can be produced for all of them WITHOUT ASR, without a GPU, and without
touching word-level data. This module is that backfill: it never invents a
verdict-computation algorithm of its own, it only drives
:func:`apps.vocals.from_stems.derive_worker_result` (the same maths
``python -m apps.vocals from-stems`` uses) and
:func:`apps.lyrics.vocal_presence.coverage_verdict` (THE canonical bands),
then writes through :func:`apps.lyrics.store.upsert_verdict` (the one
writer).

**Dependency on PR #2593** (``fix(stems): v1 bundles load MP3 and FLAC parts
by their own container``). Today ``apps.webui.server.stem_artifacts`` only
parses WAV metadata for v1 bundles, so an MP3-part bundle (915 of the 921
present tracks) fails to load with ``StemArtifactError``. This module makes
no change to that file and depends only on its public contract
(``load_stem_bundle`` returns ``StemBundle.files[part]`` paths for a loadable
v1/v3 bundle regardless of container) -- it must merge AFTER #2593, not
before. Until then every MP3-part bundle fails to load here exactly as it
does for ``from-stems``, and reports as a FAILURE line, never silently.

**Never clobbers real work.** A row already carrying ``words_content_hash``
holds real ASR/aligner word-level data, so this module SKIPS any track whose
live row already has it set at scan time, rather than writing through it --
this is a work-avoidance optimization (and an honest ``SKIP_HAS_WORDS`` line
in the report), not the only thing standing between this backfill and a
clobber. The scan's :func:`apps.lyrics.store.get_verdict` check and this
module's own :func:`apps.lyrics.store.upsert_verdict` call are two separate
statements, so a real ASR/aligner write can still land in the gap between
them. :func:`apps.lyrics.store.upsert_verdict` itself closes that window: its
``ON CONFLICT`` clause ``COALESCE``s ``n_words``/``n_lines``/
``words_content_hash`` against the row's existing values whenever this
module's coverage-only call passes ``None`` for them (see that function's
docstring), so a word row that appears mid-backfill survives even though this
module never saw it. A human ``override`` needs no special case either:
``upsert_verdict``'s ``ON CONFLICT`` clause already omits
``override``/``override_note``, so it survives every write this module
makes.

**Resumability.** Each write's ``source`` column encodes the bundle's own
identity (:func:`_bundle_source_label`: layout + the v1/v3 manifest's source
sha256), and ``pipeline_version`` is :data:`STEM_COVERAGE_PIPELINE_VERSION`.
A re-run skips any track whose live row already carries both unchanged --
the coverage is provably still current for that exact bundle generation.

**Reserved tracks.** ``<data_dir>/state/stem-order-reserved-100.json`` names
100 stable_ids held back to test in-app stem ordering; this module refuses
to process them (reports them skipped, reason "reserved") unless
``include_reserved=True`` is passed explicitly.

**Cheaper than decoding twice.** ``apps.vocals from-stems`` already computes
this exact coverage number when it fills ``data/state/vocal-cache``. When a
compatible cache entry exists (schema current, ``params.derived_from_stems``
True, its ``params.bundle_layout``/``params.bundle_source_sha256`` matching
the ON-DISK bundle's own identity, and its recorded ``duration_s`` matching
that bundle's duration within :data:`_CACHE_DURATION_TOL_S`) this module
reuses its ``coverage_pct`` instead of decoding the vocal stem + full mix a
second time. Duration alone is NOT the trust condition: a replaced bundle
with the same runtime would otherwise pass a duration-only check by
coincidence and reuse coverage computed from the old stem content, so the
bundle-identity fields must match too. The whole check stays deliberately
cheap and audio-free -- this module does not resolve rekordbox mappings or a
source audio path at all, so it works purely from the stem bundle, matching
the library scan's own ``list_bundle_ids`` contract.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apps.lyrics import store
from apps.lyrics.vocal_presence import coverage_verdict
from apps.shared.state import sync_stamp
from apps.vocals import cache as vcache
from apps.vocals import from_stems as vfrom_stems
from apps.webui.server.stem_artifacts import (
    ROFORMER_STEMS_DIR,
    StemArtifactError,
    StemBundle,
    StemBundleNotFoundError,
    load_stem_bundle,
)

#: Distinct from ``apps.lyrics.karaoke_cache.PIPELINE_VERSION``: that one
#: versions the WORD-level alignment pipeline; this constant versions only
#: the coverage-only, stem-derived verdict this module computes. Bump it
#: whenever the coverage->verdict mapping here changes, so a stale row is
#: recomputed rather than silently trusted.
STEM_COVERAGE_PIPELINE_VERSION: str = "2026.09.14-stem-coverage-v1"

RESERVED_FILENAME: str = "stem-order-reserved-100.json"

#: A vocal-cache entry is trusted as a stand-in for a fresh decode only when
#: its recorded duration matches the on-disk bundle's own duration this
#: closely. Generous enough to absorb float rounding, tight enough that a
#: replaced/differently-trimmed bundle cannot pass as "the same one".
_CACHE_DURATION_TOL_S: float = 1.0

SKIP_RESERVED: str = "reserved"
SKIP_HAS_WORDS: str = "has-word-level-data"
SKIP_UP_TO_DATE: str = "up-to-date"


@dataclass(frozen=True)
class VerdictBackfillReport:
    """What one backfill run did. Every count is observed, never inferred."""

    dry_run: bool
    data_dir: str
    candidates: int
    processed: tuple[str, ...]
    reused_cache: tuple[str, ...]
    skipped: dict[str, tuple[str, ...]] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    def skipped_counts(self) -> dict[str, int]:
        return {reason: len(ids) for reason, ids in self.skipped.items()}


#-----------------------------------------------------------------------------
# candidate discovery
#-----------------------------------------------------------------------------
def _bundle_roots(data_dir: Path) -> tuple[Path, Path]:
    """(demucs4 root, roformer2 root) for THIS ``--data-dir``.

    ``apps.webui.server.stem_artifacts.ROFORMER_STEMS_DIR`` is a module
    constant resolved from the process's ``MDT_DATA_DIR``/repo default at
    import time, NOT from a CLI ``--data-dir``. Passing it straight to
    :func:`load_stem_bundle` would silently search the wrong machine's
    RoFormer store whenever ``--data-dir`` differs from that default (exactly
    the desktop-app-vs-repo-checkout split this command exists to bridge), so
    both roots here are computed from the SAME ``data_dir`` the caller gave.
    """
    demucs_root = vfrom_stems.stems_dir(data_dir)
    roformer_root = data_dir / "state" / ROFORMER_STEMS_DIR.name
    return demucs_root, roformer_root


def _list_ids(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {child.name for child in root.iterdir() if child.is_dir() and not child.is_symlink()}


def candidate_stable_ids(data_dir: Path) -> list[str]:
    """Every stable_id with a bundle directory in EITHER root, sorted.

    Membership here only means "a directory exists" -- :func:`load_stem_bundle`
    is what actually validates it, and a bad bundle is reported as a failure
    by the caller, never filtered out silently here.
    """
    demucs_root, roformer_root = _bundle_roots(data_dir)
    return sorted(_list_ids(demucs_root) | _list_ids(roformer_root))


def load_reserved_ids(data_dir: Path) -> frozenset[str]:
    """The 100 stable_ids held back for in-app ordering QA, or empty if unset."""
    path = data_dir / "state" / RESERVED_FILENAME
    if not path.is_file():
        return frozenset()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return frozenset(str(row["stable_id"]) for row in payload["tracks"])


#-----------------------------------------------------------------------------
# coverage computation
#-----------------------------------------------------------------------------
def _bundle_source_label(bundle: StemBundle) -> str:
    """Identity string for ``lyric_verdict.source``: changes iff the bundle
    that produced this coverage number changes (new demucs run, new layout).
    This is the resumability key alongside ``pipeline_version``."""
    return f"stem-coverage:{bundle.layout}:{bundle.manifest.source.sha256}"


def _bundle_duration_s(bundle: StemBundle) -> float:
    return bundle.alignment.frame_count / bundle.alignment.sample_rate


def _cached_coverage_pct(data_dir: Path, stable_id: str, bundle: StemBundle) -> float | None:
    """A compatible ``from-stems`` vocal-cache coverage number, or None.

    Deliberately audio-free (see module docstring): this module never
    resolves a rekordbox mapping or a source audio path, so it cannot use
    :func:`apps.vocals.cache.load_valid_entry`'s mtime-signature check. A
    duration match against the ON-DISK bundle is a cheap, self-contained
    signal, but duration ALONE cannot prove two bundle generations share the
    same stems -- a replaced bundle with the same runtime would pass it by
    coincidence and reuse coverage computed from the old stem content. The
    entry's ``params.bundle_layout``/``params.bundle_source_sha256``
    (written by :func:`apps.vocals.from_stems.derive_worker_result`) are the
    bundle-identity check that closes that gap: both must match the ON-DISK
    bundle's own :attr:`StemBundle.layout` and
    ``manifest.source.sha256`` exactly. A legacy entry written before this
    check existed carries neither field, so it never matches and this module
    falls through to a real derive rather than ever trusting it blind.
    """
    path = vcache.cache_path(data_dir, stable_id)
    if not path.is_file():
        return None
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(entry, dict) or entry.get("schema") != vcache.VOCAL_CACHE_SCHEMA:
        return None
    params = entry.get("params")
    if not isinstance(params, dict) or params.get("derived_from_stems") is not True:
        return None
    if (
        params.get("bundle_layout") != bundle.layout
        or params.get("bundle_source_sha256") != bundle.manifest.source.sha256
    ):
        return None
    coverage_pct = entry.get("coverage_pct")
    duration_s = entry.get("duration_s")
    if not isinstance(coverage_pct, (int, float)) or not isinstance(duration_s, (int, float)):
        return None
    if abs(float(duration_s) - _bundle_duration_s(bundle)) > _CACHE_DURATION_TOL_S:
        return None
    return float(coverage_pct)


def coverage_pct_for_bundle(
    data_dir: Path, stable_id: str, bundle: StemBundle
) -> tuple[float, bool]:
    """(coverage_pct, reused_cache). Never reimplements the coverage maths:
    reuses a compatible vocal-cache entry, else derives it via
    :func:`apps.vocals.from_stems.derive_worker_result` (same code path as
    ``python -m apps.vocals from-stems``)."""
    cached = _cached_coverage_pct(data_dir, stable_id, bundle)
    if cached is not None:
        return cached, True
    result = vfrom_stems.derive_worker_result(bundle)
    return float(result["coverage_pct"]), False


#-----------------------------------------------------------------------------
# the backfill
#-----------------------------------------------------------------------------
def _skip(skipped: dict[str, list[str]], reason: str, stable_id: str) -> None:
    skipped.setdefault(reason, []).append(stable_id)


def _existing_row_is_current(
    row: store.LyricVerdict | None, expected_source: str
) -> bool:
    return (
        row is not None
        and row.pipeline_version == STEM_COVERAGE_PIPELINE_VERSION
        and row.source == expected_source
    )


def backfill_verdicts(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    dry_run: bool,
    limit: int | None,
    include_reserved: bool,
) -> VerdictBackfillReport:
    """Fill/refresh ``lyric_verdict`` coverage for every loadable stem bundle.

    ``limit`` caps the number of tracks actually COMPUTED (dry-run: would be
    computed) -- reserved and already-current tracks are reported but never
    count against it, matching ``apps.vocals from-stems``'s own convention.
    """
    if limit is not None and limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")
    demucs_root, roformer_root = _bundle_roots(data_dir)
    reserved = frozenset() if include_reserved else load_reserved_ids(data_dir)
    candidates = candidate_stable_ids(data_dir)

    skipped: dict[str, list[str]] = {}
    failed: dict[str, str] = {}
    processed: list[str] = []
    reused_cache: list[str] = []

    to_attempt: list[tuple[str, StemBundle, str]] = []
    for stable_id in candidates:
        if stable_id in reserved:
            _skip(skipped, SKIP_RESERVED, stable_id)
            continue
        row = store.get_verdict(conn, stable_id)
        if row is not None and row.words_content_hash is not None:
            _skip(skipped, SKIP_HAS_WORDS, stable_id)
            continue
        try:
            bundle = load_stem_bundle(stable_id, roots=(demucs_root, roformer_root))
        except (StemArtifactError, StemBundleNotFoundError) as exc:
            failed[stable_id] = f"bundle load failed: {exc}"
            continue
        expected_source = _bundle_source_label(bundle)
        if _existing_row_is_current(row, expected_source):
            _skip(skipped, SKIP_UP_TO_DATE, stable_id)
            continue
        to_attempt.append((stable_id, bundle, expected_source))

    planned = to_attempt if limit is None else to_attempt[:limit]

    if not dry_run:
        for stable_id, bundle, expected_source in planned:
            try:
                coverage_pct, reused = coverage_pct_for_bundle(data_dir, stable_id, bundle)
                store.upsert_verdict(
                    conn,
                    stable_id=stable_id,
                    verdict=coverage_verdict(coverage_pct),
                    coverage_pct=round(coverage_pct, 1),
                    source=expected_source,
                    language_iso3=None,
                    n_words=None,
                    n_lines=None,
                    pct_witness_red=None,
                    pipeline_version=STEM_COVERAGE_PIPELINE_VERSION,
                    words_content_hash=None,
                    computed_at=sync_stamp.canonical_now(),
                    resurrect=False,
                )
            except (store.LyricStoreError, sqlite3.Error, RuntimeError, ValueError) as exc:
                failed[stable_id] = f"{type(exc).__name__}: {exc}"
                continue
            processed.append(stable_id)
            if reused:
                reused_cache.append(stable_id)
    else:
        processed = [stable_id for stable_id, _bundle, _source in planned]

    return VerdictBackfillReport(
        dry_run=dry_run,
        data_dir=str(data_dir),
        candidates=len(candidates),
        processed=tuple(processed),
        reused_cache=tuple(reused_cache),
        skipped={reason: tuple(ids) for reason, ids in skipped.items()},
        failed=dict(failed),
    )


def format_report(report: VerdictBackfillReport) -> str:
    """The operator-facing summary the CLI and the parity endpoint agree on."""
    head = "DRY-RUN" if report.dry_run else "OK"
    skip_bits = ", ".join(
        f"{reason}={len(ids)}" for reason, ids in sorted(report.skipped.items())
    ) or "none"
    lines = [
        f"[{head}] lyric_verdict backfill @ {report.data_dir}: "
        f"{report.candidates} candidate bundle(s), "
        f"{len(report.processed)} processed "
        f"({len(report.reused_cache)} via cached coverage), "
        f"skipped [{skip_bits}], "
        f"{len(report.failed)} failed",
    ]
    for stable_id, reason in sorted(report.failed.items()):
        lines.append(f"[FAIL] {stable_id}: {reason}")
    return "\n".join(lines)


def report_to_dict(report: VerdictBackfillReport) -> dict[str, Any]:
    """JSON-safe shape shared by the CLI ``--json`` flag and the HTTP route."""
    return {
        "dry_run": report.dry_run,
        "data_dir": report.data_dir,
        "candidates": report.candidates,
        "processed": list(report.processed),
        "reused_cache": list(report.reused_cache),
        "skipped": {reason: list(ids) for reason, ids in report.skipped.items()},
        "failed": dict(report.failed),
    }


__all__ = [
    "RESERVED_FILENAME",
    "SKIP_HAS_WORDS",
    "SKIP_RESERVED",
    "SKIP_UP_TO_DATE",
    "STEM_COVERAGE_PIPELINE_VERSION",
    "VerdictBackfillReport",
    "backfill_verdicts",
    "candidate_stable_ids",
    "coverage_pct_for_bundle",
    "format_report",
    "load_reserved_ids",
    "report_to_dict",
]
