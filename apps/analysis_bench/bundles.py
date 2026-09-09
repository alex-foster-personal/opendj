"""Fixture bundles: the only thing that makes two hosts' rounds comparable.

WHAT A BUNDLE IS. A directory of fixture payloads (excerpt audio, ground-truth
JSON) plus three metadata files: `manifest.json` describing the set,
`SHA256SUMS` covering every file including the manifest, and `BUNDLE_ID`
carrying one digest over the payload alone. A host that pulls one can prove
byte for byte that it holds the fixtures a round was measured on, which is the
difference between "same fixtures" being checkable and being asserted.

WHY BOTH SHA256SUMS AND A BUNDLE ID. They answer different questions.
SHA256SUMS answers "did this copy arrive intact", and it covers `manifest.json`
so retitling a bundle cannot go unnoticed. BUNDLE_ID answers "is this the same
QUESTION as the bundle those numbers came from", and it deliberately excludes
the metadata files so a manifest rebuilt with a new timestamp still identifies
the same fixture set. The waveform lane learned the second half the expensive
way (Codex P1 BLOCKING on PR #1536): a version name alone let two builds with
different sample sizes both call themselves `waveform/v1`, so two rounds could
claim one fixture set while scoring different tracks.

WHY VERIFY REFUSES ON AN EXTRA FILE. A bundle carrying a payload nobody
checksummed is not this bundle, and the failure it produces downstream is a
denominator that quietly grew. Size-and-name checks are not enough: on
Tue 8 Sep 2026 a 28 percent zero-filled copy passed one.

WHERE THE STORE WENT. `push`/`pull` and the two transports live in `stores.py`.
This module answers what a bundle IS; that one answers where it travels. They
meet at `verify_bundle`, which a pull runs before it returns anything, because
a half-written bundle that still scores is the worst of the three outcomes.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path, PurePosixPath
from typing import Any

BUNDLE_SCHEMA = 2

MANIFEST_NAME = "manifest.json"
SUMS_NAME = "SHA256SUMS"
ID_NAME = "BUNDLE_ID"
METADATA_NAMES = (MANIFEST_NAME, SUMS_NAME, ID_NAME)

# Manifest keys the seal owns. Everything else in a manifest is the fixture
# DESCRIPTION, is carried forward from the staged directory, and is part of the
# bundle identity. See `semantic_manifest`.
VOLATILE_KEYS = frozenset({"built_at", "built_from"})
SEALED_KEYS = frozenset(
    {"schema", "bundle_id", "sealed_at", "payload", "payload_count", "payload_bytes"}
)

class BundleError(RuntimeError):
    """A bundle could not be sealed, verified, pushed or pulled. Never caught here."""


# ----- Hashing and identity ----------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def payload_paths(bundle: Path) -> list[str]:
    """Every file in the bundle except its own metadata, as sorted POSIX relpaths."""
    found: list[str] = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(bundle).as_posix()
        if rel in METADATA_NAMES:
            continue
        found.append(rel)
    return sorted(found)


def semantic_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """The part of a manifest that changes what a round MEASURES.

    Everything in SEALED_KEYS is either derived from the payload (`payload`,
    `payload_count`), an identity this function feeds (`bundle_id`), or a fact
    about the build rather than the question (`sealed_at`). What is left is the
    fixture description: which tracks are in, each one's scoring window, its
    stored BPM, whether its grid is dynamic. Two bundles that disagree about any
    of that are different questions even when the audio bytes are identical.
    """
    return {key: value for key, value in manifest.items() if key not in SEALED_KEYS}


def identity_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """The part of the description that belongs in the bundle id.

    `semantic_manifest` minus provenance, which records HOW a bundle was built
    rather than WHAT it asks: `built_at` moves on every rebuild and `built_from`
    is the packer's absolute path on whichever machine ran it. Hashing those
    gave a byte-identical rebuild a different id, so a round pinned with
    `--expect-bundle-id` could not recognize its own fixtures (Codex P2
    BLOCKING, PR #1582). They stay IN the manifest, where provenance belongs;
    they just do not define identity.

    The list is explicit, not heuristic. A builder that invents a new volatile
    field will move the id until that field is added here, and that is the safe
    direction to fail: an id that moves refuses a pin and someone investigates,
    where an id that failed to move would accept fixtures that score
    differently.
    """
    return {k: v for k, v in semantic_manifest(manifest).items() if k not in VOLATILE_KEYS}


def compute_bundle_id(
    lane: str,
    version: str,
    entries: list[tuple[str, str]],
    semantics: dict[str, Any] | None = None,
) -> str:
    """One digest over {lane, version, payload sha256s, fixture description}.

    Sorted and canonically encoded so two hosts building the same fixtures agree
    on the id, and a build timestamp never changes it.

    THE DESCRIPTION IS IN THE DIGEST, not just the bytes (Codex P1 BLOCKING,
    PR #1582). Hashing the payload alone let two bundles carrying identical WAVs
    but different `score_start_s`, `rb_bpm`, `is_dynamic` or fixture membership
    claim one id, so `--expect-bundle-id` would have accepted a bundle that
    scores differently -- which is the exact failure the id exists to prevent.
    """
    body = json.dumps(
        {
            "lane": lane,
            "version": version,
            "entries": sorted(entries),
            "semantics": semantics or {},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


REFERENCE_KEYS = ("wav", "audio", "path")
# `reads` is a mixed map. Only some of its entries name DATA a consumer opens
# out of the bundle; the rest are descriptive pointers. The real beatgrid
# builder writes `checksums: SHA256SUMS`, which is bundle metadata rather than a
# payload, and `scorer: apps/analysis_bench/scorers/beatgrid.py (version stamped
# ...)`, which is repository code and not a path at all. Validating every value
# rejected every real bundle the packer produces (Codex P1 BLOCKING, PR #1582),
# and the synthetic fixture missed it because it only writes `truth`. Adding a
# lane whose consumer opens a second file means adding its key here.
READS_PAYLOAD_KEYS = ("truth",)


def payload_references(description: dict[str, Any]) -> list[str]:
    """Every file a manifest tells a consumer to OPEN, as bundle-relative paths.

    Two shapes, both set by the lane fixture builders: `reads` maps a role to a
    file (`{"truth": "rekordbox-truth.json"}`), and each row of `fixtures` names
    its media under one of REFERENCE_KEYS.
    """
    reads = description.get("reads") or {}
    found = [str(reads[key]) for key in READS_PAYLOAD_KEYS if key in reads]
    rows = description.get("fixtures") or []
    if not isinstance(rows, list):
        raise BundleError(
            f"manifest `fixtures` is a {type(rows).__name__}, but this reader needs the list of "
            "fixture rows so it can check that every file they name is checksummed"
        )
    for fixture in rows:
        found.extend(str(fixture[key]) for key in REFERENCE_KEYS if key in fixture)
    return found


def require_references_inside(
    bundle: Path, description: dict[str, Any], entries: list[tuple[str, str]]
) -> None:
    """Refuse a manifest that points a consumer at bytes the checksums do not cover.

    A staged manifest carrying `reads.truth: ../truth.json`, or a fixture whose
    `wav` is an absolute path, seals and verifies happily: only files BELOW the
    bundle are hashed. The candidate and the scorer then read those outside
    bytes anyway, so one verified `bundle_id` scores differently on two hosts,
    or on the same host after somebody edits the external file (Codex P1
    BLOCKING, PR #1582). The id is a promise about what was measured; a
    reference the id does not cover breaks it.

    Both halves are required. Inside the bundle but unhashed is just as bad as
    outside it: an unlisted file is not covered by SHA256SUMS either.
    """
    listed = {rel for rel, _ in entries}
    problems: list[str] = []
    for rel in payload_references(description):
        try:
            safe_target(bundle, rel)
        except BundleError as exc:
            problems.append(str(exc))
            continue
        if rel not in listed:
            problems.append(
                f"{rel!r} is referenced by the manifest but is not one of the "
                f"{len(listed)} checksummed payload files"
            )
    if problems:
        raise BundleError(
            f"{bundle} points at bytes its own checksums do not cover, so its bundle_id "
            "would not describe what a candidate reads:\n  " + "\n  ".join(problems)
        )


# ----- Sealing and verifying ---------------------------------------------


def seal_bundle(
    bundle: Path, *, lane: str, version: str, manifest_extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Turn a staged directory of payloads into a bundle, returning its manifest."""
    bundle = Path(bundle)
    entries = [(rel, sha256_file(bundle / rel)) for rel in payload_paths(bundle)]
    if not entries:
        raise BundleError(f"{bundle} holds no payload files; refusing to seal an empty bundle")

    # A staged directory built by a lane's own fixture builder already carries a
    # manifest describing the fixtures: their scoring windows, stored BPM,
    # dynamic flag, and where the truth lives. `payload_paths` excludes that file
    # so it is not double-checksummed, which used to mean sealing SILENTLY THREW
    # IT AWAY and produced a bundle that verified and then failed the moment a
    # candidate or the scorer read it (Codex P1 BLOCKING, PR #1582). Carry it.
    staged = bundle / MANIFEST_NAME
    description: dict[str, Any] = {}
    if staged.exists():
        try:
            description = semantic_manifest(json.loads(staged.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise BundleError(
                f"{staged} is not readable JSON, so it cannot be sealed: {exc}"
            ) from exc
    description.update(manifest_extra or {})
    description["lane"] = lane
    description["version"] = version

    require_references_inside(bundle, description, entries)
    bundle_id = compute_bundle_id(lane, version, entries, identity_manifest(description))
    manifest: dict[str, Any] = {
        "schema": BUNDLE_SCHEMA,
        "bundle_id": bundle_id,
        "sealed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "payload_count": len(entries),
        "payload_bytes": sum((bundle / rel).stat().st_size for rel, _ in entries),
        "payload": [{"path": rel, "sha256": digest} for rel, digest in entries],
        **description,
    }
    (bundle / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    (bundle / ID_NAME).write_text(bundle_id + "\n", encoding="utf-8")

    lines = [f"{digest}  {rel}" for rel, digest in entries]
    lines.append(f"{sha256_file(bundle / MANIFEST_NAME)}  {MANIFEST_NAME}")
    lines.append(f"{sha256_file(bundle / ID_NAME)}  {ID_NAME}")
    (bundle / SUMS_NAME).write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")
    return manifest


def _listed_sums(bundle: Path) -> dict[str, str]:
    sums = bundle / SUMS_NAME
    if not sums.exists():
        raise BundleError(f"{bundle} carries no {SUMS_NAME}; it was never sealed")
    listed: dict[str, str] = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, rel = line.partition("  ")
        if not rel:
            raise BundleError(f"{sums} has an unparseable line: {line!r}")
        listed[rel] = digest
    return listed


def _check_bytes_against_sums(bundle: Path, listed: dict[str, str]) -> None:
    """Every listed file present and hashing right, and no unlisted file present."""
    problems: list[str] = []
    for rel, digest in sorted(listed.items()):
        target = bundle / rel
        if not target.exists():
            problems.append(f"{rel}: listed in {SUMS_NAME} but missing from the bundle")
        elif sha256_file(target) != digest:
            problems.append(f"{rel}: on disk it hashes differently from {SUMS_NAME}")
    on_disk = set(payload_paths(bundle)) | {MANIFEST_NAME, ID_NAME}
    problems.extend(
        f"{rel}: present in the bundle but not listed in {SUMS_NAME}"
        for rel in sorted(on_disk - set(listed))
    )
    if problems:
        raise BundleError(f"{bundle} failed verification:\n  " + "\n  ".join(problems))


def _bound_entries(
    bundle: Path, manifest: dict[str, Any], listed: dict[str, str]
) -> list[tuple[str, str]]:
    """The payload entries, proved to be the same list in SHA256SUMS and the manifest.

    `_check_bytes_against_sums` proves every file matches SHA256SUMS, and the id
    is recomputed from `manifest["payload"]`. WITHOUT THIS COMPARISON THE TWO
    NEVER MEET: a bundle whose audio and SHA256SUMS were both replaced, keeping
    the original manifest and BUNDLE_ID, verified and then satisfied
    `--expect-bundle-id` for the old fixtures. The pin defeated by the very
    files it pins (Codex P1 BLOCKING, PR #1582).
    """
    entries = sorted((rel, digest) for rel, digest in listed.items() if rel not in METADATA_NAMES)
    stated = sorted((entry["path"], entry["sha256"]) for entry in manifest["payload"])
    if entries == stated:
        return entries
    only_sums = sorted(rel for rel, _ in set(entries) - set(stated))
    only_manifest = sorted(rel for rel, _ in set(stated) - set(entries))
    raise BundleError(
        f"{bundle} manifest payload list disagrees with {SUMS_NAME}: "
        f"{len(only_sums)} entr(ies) only in {SUMS_NAME} (first {only_sums[:1]}), "
        f"{len(only_manifest)} only in the manifest (first {only_manifest[:1]}). "
        "One of the two was replaced without the other."
    )


def verify_bundle(bundle: Path, *, expect_bundle_id: str | None = None) -> dict[str, Any]:
    """Prove this directory is the bundle it claims to be, or raise saying why."""
    bundle = Path(bundle)
    listed = _listed_sums(bundle)
    _check_bytes_against_sums(bundle, listed)

    manifest = json.loads((bundle / MANIFEST_NAME).read_text(encoding="utf-8"))
    if manifest.get("schema") != BUNDLE_SCHEMA:
        raise BundleError(
            f"{bundle} is bundle schema {manifest.get('schema')!r}, this reader is {BUNDLE_SCHEMA}"
        )
    entries = _bound_entries(bundle, manifest, listed)
    require_references_inside(bundle, manifest, entries)
    recomputed = compute_bundle_id(
        manifest["lane"], manifest["version"], entries, identity_manifest(manifest)
    )
    on_file = (bundle / ID_NAME).read_text(encoding="utf-8").strip()
    if recomputed != manifest["bundle_id"] or recomputed != on_file:
        raise BundleError(
            f"{bundle} bundle_id disagrees with its own payload: manifest "
            f"{manifest['bundle_id']}, {ID_NAME} {on_file}, recomputed {recomputed}"
        )
    if expect_bundle_id is not None and expect_bundle_id != recomputed:
        raise BundleError(
            f"{bundle} is bundle_id {recomputed}, but {expect_bundle_id} was required; "
            "these are different fixture sets sharing a version name"
        )
    return manifest


# ----- Path safety --------------------------------------------------------


def safe_target(dest: Path, rel: str) -> Path:
    """Resolve one store key suffix under `dest`, or refuse to write outside it.

    A store is not trusted merely because we hold its credentials. An object key
    ending `../../workspace/file` would otherwise be joined straight onto the
    destination and `download_file` would write OUTSIDE the bundle directory,
    where the verify-then-remove cleanup on a failed pull never reaches (Codex
    P1 BLOCKING, PR #1582). Both checks are kept: the parts check names what is
    wrong, the resolve check is the proof.
    """
    parts = PurePosixPath(rel).parts
    if not rel or rel.startswith("/") or PurePosixPath(rel).is_absolute() or ".." in parts:
        raise BundleError(
            f"store key suffix {rel!r} is absolute or climbs out of the bundle; "
            f"refusing to write it under {dest}"
        )
    target = (dest / rel).resolve()
    root = dest.resolve()
    if root != target and root not in target.parents:
        raise BundleError(
            f"store key suffix {rel!r} resolves to {target}, outside {root}; refusing to write it"
        )
    return target
