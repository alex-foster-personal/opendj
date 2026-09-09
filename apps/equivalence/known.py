"""Step 5: the known-answer harness.

"Known-answer spot checks on about 10 tracks a human can verify by ear or by
reading the source app's own UI. This is the only step that catches a mapping
that is internally consistent but wrong." (SKILL 4b)

Honesty rules baked into the fixture format, because a fabricated expected
value turns this step into theatre:

* ``verified_by: "source-read"`` -- the expected value was READ OUT of the
  store on the recorded date. It pins our reader and our normaliser against
  regression. It does NOT establish that the source is right about the track.
* ``verified_by: "audit-table"`` -- the value also appears in the
  MIK-AUDIT side-by-side table, so it has had human eyes on it.
* ``verified_by: "needs-maintainer"`` -- expected is ``null`` and stays null until a
  human settles it by ear or by reading the app's own UI. These report as
  PENDING, never as pass, and they never contribute to a passing verdict.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from apps.equivalence.config import (
    FIELD_PAIRS,
    FIXTURE_MANIFEST_PATH,
    FIXTURE_PATH,
    SINGLE_SOURCE_FIELDS,
    SourceField,
)
from apps.equivalence.normalisers import MISSING, Key, NormaliseError, normalise
from apps.equivalence.sources import MikRow, Pairing

VERIFIED_BY = ("source-read", "audit-table", "needs-maintainer")
FIXTURE_SCHEMA = "equivalence-known-answers/v1"


class FixtureError(ValueError):
    """The fixture is malformed. Never skipped, never defaulted."""


@dataclass
class CheckResult:
    track_id: str
    target: str
    expected_raw: Any
    actual_raw: Any
    expected_canonical: str | None
    actual_canonical: str | None
    verified_by: str
    status: str  # 'pass' | 'fail' | 'pending' | 'unresolved'
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "target": self.target,
            "expected_raw": self.expected_raw,
            "actual_raw": self.actual_raw,
            "expected_canonical": self.expected_canonical,
            "actual_canonical": self.actual_canonical,
            "verified_by": self.verified_by,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class KnownAnswerReport:
    fixture_path: str
    tracks: int
    passed: int
    failed: int
    pending: int
    unresolved: int
    results: list[CheckResult]

    @property
    def unresolved_checks(self) -> int:
        """Checks whose TARGET could not be resolved, so they never ran."""
        return sum(1 for r in self.results if r.status == "unresolved")

    @property
    def ok(self) -> bool:
        """True only if nothing failed, nothing is pending, and everything resolved.

        ``unresolved`` used to count tracks only, so a check whose target
        column stopped being declared vanished from the pass/fail arithmetic
        while still printing a row in the report. Moving ``energy`` between two
        config lists silently disabled ten checks that way on Tue 28 Jul 2026.
        A check that did not run is not a check that passed.

        ``pending`` (``verified_by: needs-maintainer``) is not a failure, but it is
        not a pass either: it is a human verification this suite's own CLI
        contract requires before a run is trustworthy. Treating it as
        "no failures, so ok" let a run with pending checks still publish the
        canonical gate and exit 0.
        """
        return (
            self.failed == 0
            and self.pending == 0
            and self.unresolved_checks == 0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "fixture": self.fixture_path,
            "tracks": self.tracks,
            "passed": self.passed,
            "failed": self.failed,
            "pending_human_verification": self.pending,
            "unresolved_tracks": self.unresolved,
            "unresolved_checks": self.unresolved_checks,
            "ok": self.ok,
            "results": [r.as_dict() for r in self.results],
            "note": (
                "'pending' entries carry expected: null and verified_by: "
                "needs-maintainer. They require the maintainer's ear or a look at the app UI "
                "and are deliberately NOT counted as passes."
            ),
        }


# --------------------------------------------------------------- lookup


# attr -> the SourceField declaring its kind and unit, per source.
def _spec_index() -> dict[tuple[str, str], SourceField]:
    """Every declared column, whether it is half of a PAIR or source-unique.

    SINGLE_SOURCE_FIELDS is included deliberately. When ``energy`` moved out of
    FIELD_PAIRS on Tue 28 Jul 2026 this index silently lost ``mik.energy``, and
    the fixture's ten energy expectations degraded to ``unresolved`` -- they
    stopped being checked while still looking like rows in the report. A
    declaration moving between two lists must never quietly disable a
    known-answer check, so the index reads both lists.
    """
    index: dict[tuple[str, str], SourceField] = {}
    for pair in FIELD_PAIRS:
        for side in (pair.left, pair.right):
            index[(side.source, side.attr)] = side
    for spec in SINGLE_SOURCE_FIELDS:
        index.setdefault((spec.source.source, spec.source.attr), spec.source)
    return index


def _canonical_text(value: Any, spec: SourceField) -> str | None:
    try:
        result = normalise(value, spec.kind, spec.unit)
    except NormaliseError as exc:
        return f"UNMAPPED ({exc.reason})"
    if result is MISSING:
        return "MISSING"
    if isinstance(result, Key):
        return result.camelot
    return f"{float(result):g}"


def _find_mik(mik_rows: list[MikRow], selector: dict[str, Any]) -> MikRow | None:
    """Resolve a fixture selector to one MIK row, or None.

    ``MikRow`` carries ``path``, not a basename, so the basename selector
    derives it. An earlier version read ``row.file_name`` and raised
    AttributeError on every basename selector.
    """
    basename = selector.get("mik_basename")
    title = selector.get("mik_title")
    if not basename and not title:
        raise FixtureError(
            f"selector {selector!r} names neither 'mik_basename' nor 'mik_title'"
        )
    for row in mik_rows:
        if title and row.title == title:
            return row
        if basename and row.path and PurePosixPath(row.path).name == basename:
            return row
    return None


# ---------------------------------------------------------------- harness


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    """The locked ``{schema, sha256}`` declaration for a known-answer fixture.

    A missing manifest is a hard error, not an unverified pass: without it
    there is nothing to check the fixture's checksum AGAINST, and treating
    that as "fine" is exactly the gap this closes -- an unreviewed edit to
    ``known_answers.json`` would then be indistinguishable from the reviewed
    fixture and could bless a gate run on fabricated expected values.
    """
    if not manifest_path.exists():
        raise FixtureError(
            f"known-answer fixture manifest not found: {manifest_path}. "
            f"Regenerate with `python -m apps.equivalence.known "
            f"--write-manifest` after a REVIEWED change to the fixture."
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FixtureError(f"{manifest_path}: not valid JSON: {exc}") from exc
    if "schema" not in manifest or "sha256" not in manifest:
        raise FixtureError(
            f"{manifest_path}: manifest must declare 'schema' and 'sha256'"
        )
    return manifest


def write_manifest(
    path: Path = FIXTURE_PATH, manifest_path: Path = FIXTURE_MANIFEST_PATH
) -> dict[str, Any]:
    """Regenerate the locked manifest from the CURRENT fixture on disk.

    Only ever run this after the fixture change itself has been reviewed --
    it is the thing that turns an edit into the new canonical declaration,
    not a way to silence a checksum mismatch.
    """
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    manifest = {"schema": payload.get("schema"), "sha256": _sha256(raw)}
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _verify_checksum(path: Path, manifest_path: Path, raw: bytes) -> None:
    """The manifest half of ``load_fixture``'s validation, split out to keep
    ``load_fixture`` under the complexity ceiling. See its docstring for why
    both schema AND checksum are checked here."""
    manifest = _load_manifest(manifest_path)
    if manifest["schema"] != FIXTURE_SCHEMA:
        raise FixtureError(
            f"{manifest_path}: schema {manifest['schema']!r}, expected "
            f"{FIXTURE_SCHEMA!r}"
        )
    actual_sha256 = _sha256(raw)
    if manifest["sha256"] != actual_sha256:
        raise FixtureError(
            f"{path}: checksum {actual_sha256} does not match the locked "
            f"manifest {manifest_path} ({manifest['sha256']}). If this edit "
            f"to the fixture was reviewed, regenerate the manifest with "
            f"`python -m apps.equivalence.known --write-manifest`; if it was "
            f"not, the fixture has been changed without review."
        )


def _validate_tracks(path: Path, tracks: Any) -> None:
    """The track-shape half of ``load_fixture``'s validation, split out to
    keep ``load_fixture`` under the complexity ceiling."""
    if not isinstance(tracks, list) or not tracks:
        raise FixtureError(f"{path}: 'tracks' must be a non-empty list")
    for track in tracks:
        if "id" not in track or "select" not in track:
            raise FixtureError(f"{path}: every track needs 'id' and 'select'")
        for check in track.get("expect", []):
            _validate_check(path, track, check)


def _validate_check(path: Path, track: dict[str, Any], check: dict[str, Any]) -> None:
    if check.get("verified_by") not in VERIFIED_BY:
        raise FixtureError(
            f"{path}: track {track['id']!r} has verified_by "
            f"{check.get('verified_by')!r}, expected one of {VERIFIED_BY}"
        )
    if check["verified_by"] == "needs-maintainer" and check.get("raw") is not None:
        raise FixtureError(
            f"{path}: track {track['id']!r} is marked needs-maintainer but "
            f"carries an expected value; that is a fabricated answer"
        )
    if check["verified_by"] != "needs-maintainer" and "raw" not in check:
        raise FixtureError(
            f"{path}: track {track['id']!r} check {check.get('target')!r} "
            f"has no 'raw' expectation"
        )


def load_fixture(
    path: Path = FIXTURE_PATH, *, manifest_path: Path | None = None
) -> dict[str, Any]:
    """Read and validate the fixture. A malformed fixture is a hard error.

    The default known-answer fixture is release-gating evidence (P1 regression,
    PR #383 review): validating only the embedded ``schema`` string cannot
    catch a convenience edit to the expected answers, since the edited file
    still declares the same schema and is otherwise well-formed. So the
    fixture's version AND its checksum are checked against the locked
    manifest at ``manifest_path`` -- a fixture that does not match the
    canonical declaration byte-for-byte is rejected outright, never loaded
    with a warning.

    ``manifest_path`` defaults to ``path`` with its suffix replaced by
    ``.manifest.json`` (NOT to the checked-in fixture's own manifest
    unconditionally), so a caller pointing ``path`` at a different fixture
    -- ``apps.equivalence run --fixture`` supports exactly this -- is
    checked against that fixture's own sibling manifest, not silently
    against ``known_answers.manifest.json``.
    """
    if not path.exists():
        raise FixtureError(f"known-answer fixture not found: {path}")
    if manifest_path is None:
        manifest_path = path.with_suffix(".manifest.json")
    raw = path.read_bytes()
    _verify_checksum(path, manifest_path, raw)
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("schema") != FIXTURE_SCHEMA:
        raise FixtureError(
            f"{path}: schema {payload.get('schema')!r}, expected {FIXTURE_SCHEMA!r}"
        )
    _validate_tracks(path, payload.get("tracks"))
    return payload


def run_known_answers(
    mik_rows: list[MikRow],
    pairings: list[Pairing],
    *,
    fixture_path: Path = FIXTURE_PATH,
) -> KnownAnswerReport:
    """Check every fixture expectation against what the readers actually return."""
    payload = load_fixture(fixture_path)
    specs = _spec_index()
    by_mik_pk = {p.right.pk: p for p in pairings}
    results: list[CheckResult] = []
    unresolved = 0

    for track in payload["tracks"]:
        track_id = str(track["id"])
        mik = _find_mik(mik_rows, track["select"])
        if mik is None:
            unresolved += 1
            results.append(
                CheckResult(
                    track_id=track_id,
                    target="(track)",
                    expected_raw=track["select"],
                    actual_raw=None,
                    expected_canonical=None,
                    actual_canonical=None,
                    verified_by="source-read",
                    status="unresolved",
                    detail="no MIK row matched this selector",
                )
            )
            continue
        pairing = by_mik_pk.get(mik.pk)
        results.extend(
            _run_check(track_id, check, mik, pairing, specs)
            for check in track.get("expect", [])
        )

    return KnownAnswerReport(
        fixture_path=str(fixture_path),
        tracks=len(payload["tracks"]),
        passed=sum(1 for r in results if r.status == "pass"),
        failed=sum(1 for r in results if r.status == "fail"),
        pending=sum(1 for r in results if r.status == "pending"),
        unresolved=unresolved,
        results=results,
    )


def _run_check(
    track_id: str,
    check: dict[str, Any],
    mik: MikRow,
    pairing: Pairing | None,
    specs: dict[tuple[str, str], SourceField],
) -> CheckResult:
    target = str(check["target"])
    source, _, attr = target.partition(".")
    spec = specs.get((source, attr))
    verified_by = str(check["verified_by"])

    if spec is None:
        return CheckResult(
            track_id,
            target,
            check.get("raw"),
            None,
            None,
            None,
            verified_by,
            "unresolved",
            f"no declared field for target {target!r}",
        )
    if source == "mik":
        row: Any = mik
    elif source == "rekordbox":
        if pairing is None:
            return CheckResult(
                track_id,
                target,
                check.get("raw"),
                None,
                None,
                None,
                verified_by,
                "unresolved",
                "MIK row matched no rekordbox row, so there is "
                "nothing to check on the rekordbox side",
            )
        row = pairing.left
    else:
        return CheckResult(
            track_id,
            target,
            check.get("raw"),
            None,
            None,
            None,
            verified_by,
            "unresolved",
            f"unknown source {source!r}",
        )

    actual_raw = getattr(row, attr, None)
    actual_canonical = _canonical_text(actual_raw, spec)

    if verified_by == "needs-maintainer":
        return CheckResult(
            track_id,
            target,
            None,
            actual_raw,
            None,
            actual_canonical,
            verified_by,
            "pending",
            check.get("why", "awaiting human verification"),
        )

    expected_raw = check["raw"]
    expected_canonical = check.get("canonical")
    raw_ok = _loose_equal(expected_raw, actual_raw)
    canon_ok = expected_canonical is None or expected_canonical == actual_canonical
    if raw_ok and canon_ok:
        return CheckResult(
            track_id,
            target,
            expected_raw,
            actual_raw,
            expected_canonical,
            actual_canonical,
            verified_by,
            "pass",
        )
    return CheckResult(
        track_id,
        target,
        expected_raw,
        actual_raw,
        expected_canonical,
        actual_canonical,
        verified_by,
        "fail",
        "raw mismatch" if not raw_ok else "canonical mismatch",
    )


def _loose_equal(expected: Any, actual: Any) -> bool:
    """Numeric expectations compare as floats; everything else compares as-is."""
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(expected) - float(actual)) < 1e-6
    return expected == actual


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help=(
            "regenerate the locked manifest from the fixture currently on "
            "disk. Only run this after the fixture change has been reviewed."
        ),
    )
    args = parser.parse_args()
    if not args.write_manifest:
        parser.print_help()
        return 1
    manifest = write_manifest()
    print(f"wrote {FIXTURE_MANIFEST_PATH}: {manifest}")
    return 0


__all__ = [
    "FIXTURE_SCHEMA",
    "VERIFIED_BY",
    "CheckResult",
    "FixtureError",
    "KnownAnswerReport",
    "load_fixture",
    "run_known_answers",
    "write_manifest",
]

if __name__ == "__main__":
    raise SystemExit(_main())
