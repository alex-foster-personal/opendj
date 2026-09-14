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
then writes through :func:`apps.lyrics.store.upsert_stem_coverage_verdict`
-- a dedicated, atomically-guarded writer, NOT the shared
:func:`apps.lyrics.store.upsert_verdict` every other lyrics writer uses (see
**Never clobbers real work** below for why this module needs its own).

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
(real ASR/aligner word-level data) OR a human ``override`` is excluded from
this backfill ENTIRELY -- not merely protected column-by-column. LYR-06's
own acceptance criterion groups the two together: "the backfill never
overwrites it, only fills/refreshes coverage-only rows." A row is SKIPPED at
scan time (``SKIP_HAS_WORDS`` / ``SKIP_HAS_OVERRIDE``) the moment either
field is set, rather than writing through it -- this is a work-avoidance
optimization, not the only thing standing between this backfill and a
clobber. The scan's :func:`apps.lyrics.store.get_verdict` check and this
module's own write are two separate statements, so a real ASR/aligner write
or a fresh human override can still land in the gap between them.

An earlier revision tried closing that gap inside the SHARED
:func:`apps.lyrics.store.upsert_verdict` writer, by ``COALESCE``-preserving
just the three word columns (``n_words``/``n_lines``/``words_content_hash``)
whenever this module's call passed ``None`` for them. That left every OTHER
column this module writes -- ``verdict``, ``coverage_pct``, ``source``,
``language_iso3``, ``pct_witness_red``, ``pipeline_version``,
``computed_at`` -- still exposed to the exact same race: a word-level row
would keep its words, but with a stem-only verdict and coverage number
stamped over the real ASR judgement. A later revision moved to a dedicated
:func:`apps.lyrics.store.upsert_stem_coverage_verdict` writer whose conflict
update was conditional on ``words_content_hash IS NULL`` alone -- closing the
word-level race, but Sol's follow-up review pointed out the identical race
still existed for ``override``: the SCAN check excluded an overridden row,
but nothing guarded the WRITE against a fresh ``set_override`` landing in
the same gap, so an overridden row's computed fields (not the ``override``
column itself, which survives structurally via the ``ON CONFLICT`` set-list
omission either way) could still get silently recomputed underneath the
human's decision. The writer's conditional guard now covers BOTH columns in
the SAME statement (``WHERE lyric_verdict.words_content_hash IS NULL AND
lyric_verdict.override IS NULL``, checked inside the SAME atomic write, not
as a separate prior read) -- so a row that has gone word-level OR gained an
override in that gap is left WHOLLY untouched, not partially protected. The
write reports back whether it actually landed; a race loss is counted as
:data:`SKIP_PROTECTED_ROW_RACE`, not silently folded into ``processed``.
``upsert_verdict`` itself is unchanged -- the shared writer every other
caller (:mod:`apps.lyrics.ingest_state`, :mod:`apps.lyrics.legacy_words`)
relies on keeps its original semantics.

**Resumability.** Each write's ``source`` column encodes the STEMS'
identity (:func:`_bundle_source_label`: layout +
:func:`apps.vocals.from_stems.bundle_stem_identity`), and
``pipeline_version`` is :data:`STEM_COVERAGE_PIPELINE_VERSION`. A re-run
skips any track whose live row already carries both unchanged -- the
coverage is provably still current for that exact bundle generation. This is
deliberately NOT keyed on ``bundle.manifest.source.sha256`` alone: that
identifies only the ORIGINAL track and stays fixed across a re-separation
with a different model, version, or a single repaired part, which would
otherwise leave a stale verdict silently unrefreshed forever (see
``bundle_stem_identity``'s docstring).

**Reserved tracks.** ``<data_dir>/state/stem-order-reserved-100.json`` names
100 stable_ids held back to test in-app stem ordering; this module refuses
to process them (reports them skipped, reason "reserved") unless
``include_reserved=True`` is passed explicitly.

**Cheaper than decoding twice.** ``apps.vocals from-stems`` already computes
this exact coverage number when it fills ``data/state/vocal-cache``. When a
compatible cache entry exists (schema current, ``params.derived_from_stems``
True, its ``params.bundle_layout``/``params.bundle_stem_sha256`` matching
the ON-DISK bundle's own identity, its recorded ``duration_s`` matching that
bundle's duration within :data:`_CACHE_DURATION_TOL_S`, and both values
finite and in range) this module reuses its ``coverage_pct`` instead of
decoding the vocal stem + full mix a second time. Duration alone is NOT the
trust condition: a replaced bundle with the same runtime would otherwise
pass a duration-only check by coincidence and reuse coverage computed from
the old stem content, so the bundle-identity fields must match too. The
whole check stays deliberately cheap and audio-free -- this module does not
resolve rekordbox mappings or a source audio path at all, so it works purely
from the stem bundle, matching the library scan's own ``list_bundle_ids``
contract.
"""

from __future__ import annotations

import json
import math
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
#: LYR-06's acceptance criterion groups a human ``override`` with
#: ``words_content_hash``: both exclude a row from this backfill entirely,
#: not merely from having ``override``/``override_note`` themselves
#: overwritten. ``set_override`` only ever applies to a row that already has
#: a computed verdict, so this check (like ``SKIP_HAS_WORDS``) is always
#: scanning a real, pre-existing row.
SKIP_HAS_OVERRIDE: str = "has-human-override"
SKIP_UP_TO_DATE: str = "up-to-date"
#: The scan-phase :data:`SKIP_HAS_WORDS` / :data:`SKIP_HAS_OVERRIDE` checks
#: missed this row (it was coverage-only, or absent, when scanned), but a
#: real ASR/aligner write OR a fresh human override landed on it before this
#: module's own write reached it. Distinct from those two: they are the
#: cheap, expected, common case caught up front; this is the rare race
#: counted separately so it is never silently folded into ``processed``. See
#: :func:`apps.lyrics.store.upsert_stem_coverage_verdict`.
SKIP_PROTECTED_ROW_RACE: str = "protected-row-race"


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
    """Identity string for ``lyric_verdict.source``: changes iff the STEMS
    that produced this coverage number change -- keyed on
    :func:`apps.vocals.from_stems.bundle_stem_identity`, not
    ``bundle.manifest.source.sha256`` (which identifies only the ORIGINAL
    track and stays fixed across a re-separation with a different model,
    version, or a single repaired part -- see that function's docstring for
    why that distinction matters here). This is the resumability key
    alongside ``pipeline_version``."""
    return f"stem-coverage:{bundle.layout}:{vfrom_stems.bundle_stem_identity(bundle)}"


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
    entry's ``params.bundle_layout``/``params.bundle_stem_sha256`` (written
    by :func:`apps.vocals.from_stems.derive_worker_result`, keyed on
    :func:`apps.vocals.from_stems.bundle_stem_identity`) are the
    bundle-identity check that closes that gap: both must match the ON-DISK
    bundle's own :attr:`StemBundle.layout` and freshly-computed stem
    identity exactly, so a re-separation with a new model/version or a
    single repaired part is caught, not only a fully replaced bundle. A
    legacy entry written before this check existed carries neither field, so
    it never matches and this module falls through to a real derive rather
    than ever trusting it blind.

    Also refuses a malformed or out-of-range entry outright: ``json.loads``
    happily parses ``NaN``/``Infinity`` (valid JS, invalid JSON) as Python
    floats that pass a bare ``isinstance(x, (int, float))`` check, and a NaN
    ``duration_s`` would then pass the tolerance comparison below too
    (every comparison against NaN is False, so ``> _CACHE_DURATION_TOL_S``
    is never true). Both values must be finite, ``coverage_pct`` must sit in
    ``[0, 100]``, and ``duration_s`` must be positive -- anything else falls
    through to a real derive exactly like an INCOMPATIBLE entry (wrong
    schema, mismatched bundle identity): the file read fine, its content
    just is not reusable, so silently deriving instead is the honest
    behaviour.

    NOTHING at the path (no prior ``from-stems`` run for this track) is a
    third, equally legitimate ``None`` case -- there was never anything to
    read. Checked with :meth:`Path.exists`, not :meth:`Path.is_file`: the
    latter would ALSO return False for a directory sitting where a file is
    expected, silently folding that anomaly into the same "nothing to read"
    path instead of surfacing it below.

    Anything present at the path but not a normally readable file is
    different in kind from either of those, and is NOT folded into the same
    ``None`` path: a permission error, a truncated write, corrupt JSON, or a
    directory where a file is expected means something is actually wrong on
    disk, not merely "no cache yet". Silently treating that as a miss and
    deriving fresh coverage would still produce a technically correct
    verdict, but it would mask the underlying fault from the operator on
    every affected track, forever -- the project's fail-fast rule against
    silent exception handling. This raises instead, which
    :func:`backfill_verdicts`'s existing per-track exception handler turns
    into an honest FAILURE line rather than a quiet, indistinguishable
    re-derive.
    """
    path = vcache.cache_path(data_dir, stable_id)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"vocal-cache entry for {stable_id!r} exists at {path} but could "
            f"not be read: {exc}"
        ) from exc
    try:
        entry = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"vocal-cache entry for {stable_id!r} exists at {path} but is "
            f"not valid JSON: {exc}"
        ) from exc
    if (
        not isinstance(entry, dict)
        or entry.get("schema") != vcache.VOCAL_CACHE_SCHEMA
        or not isinstance((params := entry.get("params")), dict)
        or params.get("derived_from_stems") is not True
    ):
        return None
    try:
        live_identity = vfrom_stems.bundle_stem_identity(bundle)
        stem_identity_matches = params.get("bundle_stem_sha256") == live_identity
    except OSError as exc:
        # Same second-stat-pass hazard as ``_bundle_source_label`` (see its
        # comment in ``_scan_candidate``): the bundle loaded fine earlier,
        # but a part vanished/became unreadable before this cache-identity
        # comparison re-stat'd it. Raise, matching this function's existing
        # pattern for its OTHER filesystem read (the vocal-cache file
        # itself, above) -- ``backfill_verdicts``'s write loop already
        # catches ``RuntimeError`` and records a per-track FAILURE instead
        # of aborting the run.
        raise RuntimeError(
            f"stem bundle for {stable_id!r} could not be re-checked for cache "
            f"identity: {exc}"
        ) from exc
    if params.get("bundle_layout") != bundle.layout or not stem_identity_matches:
        return None
    coverage_pct = entry.get("coverage_pct")
    duration_s = entry.get("duration_s")
    if not isinstance(coverage_pct, (int, float)) or not isinstance(duration_s, (int, float)):
        return None
    coverage_pct = float(coverage_pct)
    duration_s = float(duration_s)
    if (
        not math.isfinite(coverage_pct)
        or not math.isfinite(duration_s)
        or not (0.0 <= coverage_pct <= 100.0)
        or duration_s <= 0.0
    ):
        return None
    if abs(duration_s - _bundle_duration_s(bundle)) > _CACHE_DURATION_TOL_S:
        return None
    return coverage_pct


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


@dataclass(frozen=True)
class _ScanOutcome:
    """One candidate's scan-phase classification: exactly one field is set.
    Pulled out of :func:`backfill_verdicts` so that function's own branching
    stays flat -- a caller-facing loop over ``if outcome.skip_reason /
    outcome.fail_reason / else`` instead of every one of these checks inline.
    """

    skip_reason: str | None = None
    fail_reason: str | None = None
    attempt: tuple[str, StemBundle, str] | None = None


def _scan_candidate(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    reserved: frozenset[str],
    demucs_root: Path,
    roformer_root: Path,
) -> _ScanOutcome:
    """Classify one candidate stable_id: reserved / protected (override or
    word-level) / a failed bundle load / already current / needs a real
    recompute. Never loads a bundle for a reserved or protected row -- both
    checks are cheap and must fire before the more expensive load."""
    if stable_id in reserved:
        return _ScanOutcome(skip_reason=SKIP_RESERVED)
    row = store.get_verdict(conn, stable_id)
    if row is not None and row.override is not None:
        return _ScanOutcome(skip_reason=SKIP_HAS_OVERRIDE)
    if row is not None and row.words_content_hash is not None:
        return _ScanOutcome(skip_reason=SKIP_HAS_WORDS)
    try:
        bundle = load_stem_bundle(stable_id, roots=(demucs_root, roformer_root))
    except (StemArtifactError, StemBundleNotFoundError) as exc:
        return _ScanOutcome(fail_reason=f"bundle load failed: {exc}")
    return _classify_loaded_bundle(stable_id, row, bundle)


def _classify_loaded_bundle(
    stable_id: str, row: store.LyricVerdict | None, bundle: StemBundle
) -> _ScanOutcome:
    """The post-load half of :func:`_scan_candidate`, split out as its own
    seam: ``load_stem_bundle`` succeeding proves the bundle was loadable AT
    THAT INSTANT, but ``_bundle_source_label`` below re-stats the manifest
    plus every part file a SECOND time (see ``bundle_stem_identity``'s
    docstring), and single-threaded Python offers no way to pause
    :func:`_scan_candidate` between its own internal load and this step to
    inject a real filesystem mutation in between. Pulling this out lets a
    caller (production or test) load a bundle for real, mutate the
    filesystem for real, then call this directly -- exercising the exact
    post-load re-stat this function performs without needing to fabricate
    or mock anything."""
    try:
        expected_source = _bundle_source_label(bundle)
    except OSError as exc:
        # A part removed, made unreadable, or replaced by a directory in the
        # interval between the successful load and this re-stat is a genuine
        # filesystem fault, not a "bundle doesn't exist" case: report it as
        # a per-track FAILURE (never a silent skip) and let the rest of the
        # run continue.
        return _ScanOutcome(fail_reason=f"bundle filesystem check failed: {exc}")
    if _existing_row_is_current(row, expected_source):
        return _ScanOutcome(skip_reason=SKIP_UP_TO_DATE)
    return _ScanOutcome(attempt=(stable_id, bundle, expected_source))


@dataclass(frozen=True)
class _WriteOutcome:
    """One candidate's write-phase result: exactly one of ``fail_reason`` /
    ``race_lost`` is set, or neither (a plain successful write). Pulled out
    of :func:`backfill_verdicts` -- like :class:`_ScanOutcome` on the scan
    side -- so the single-candidate write step is independently callable: a
    test can hand it a candidate whose ``(bundle, expected_source)`` was
    captured BEFORE a real concurrent write landed on the row, without going
    through the whole scan/plan machinery again."""

    fail_reason: str | None = None
    race_lost: bool = False
    reused_cache: bool = False


def _write_candidate(
    conn: sqlite3.Connection,
    data_dir: Path,
    stable_id: str,
    bundle: StemBundle,
    expected_source: str,
) -> _WriteOutcome:
    """Compute coverage for one scanned candidate and write it through the
    atomic, protected-row-race-aware writer. Never raises for an expected
    per-track failure (filesystem, store, or malformed-cache) -- those come
    back as :attr:`_WriteOutcome.fail_reason` so the caller can keep
    processing the REST of the run instead of aborting it."""
    try:
        coverage_pct, reused = coverage_pct_for_bundle(data_dir, stable_id, bundle)
        written = store.upsert_stem_coverage_verdict(
            conn,
            stable_id=stable_id,
            verdict=coverage_verdict(coverage_pct),
            coverage_pct=round(coverage_pct, 1),
            source=expected_source,
            pipeline_version=STEM_COVERAGE_PIPELINE_VERSION,
            computed_at=sync_stamp.canonical_now(),
        )
    except (store.LyricStoreError, sqlite3.Error, RuntimeError, ValueError) as exc:
        return _WriteOutcome(fail_reason=f"{type(exc).__name__}: {exc}")
    if not written:
        # A real ASR/aligner write OR a fresh human override landed on this
        # row after the scan passed it (SKIP_HAS_WORDS / SKIP_HAS_OVERRIDE
        # look stale-but-honest here) and before this write reached it.
        # upsert_stem_coverage_verdict already left the row wholly untouched;
        # this is only the bookkeeping for that outcome.
        return _WriteOutcome(race_lost=True)
    return _WriteOutcome(reused_cache=reused)


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
        outcome = _scan_candidate(
            conn,
            stable_id,
            reserved=reserved,
            demucs_root=demucs_root,
            roformer_root=roformer_root,
        )
        if outcome.skip_reason is not None:
            _skip(skipped, outcome.skip_reason, stable_id)
        elif outcome.fail_reason is not None:
            failed[stable_id] = outcome.fail_reason
        else:
            assert outcome.attempt is not None
            to_attempt.append(outcome.attempt)

    planned = to_attempt if limit is None else to_attempt[:limit]

    if not dry_run:
        for stable_id, bundle, expected_source in planned:
            outcome = _write_candidate(conn, data_dir, stable_id, bundle, expected_source)
            if outcome.fail_reason is not None:
                failed[stable_id] = outcome.fail_reason
                continue
            if outcome.race_lost:
                _skip(skipped, SKIP_PROTECTED_ROW_RACE, stable_id)
                continue
            processed.append(stable_id)
            if outcome.reused_cache:
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
    "SKIP_HAS_OVERRIDE",
    "SKIP_HAS_WORDS",
    "SKIP_PROTECTED_ROW_RACE",
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
