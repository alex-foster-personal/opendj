"""What a ``waveform/<version>`` bundle's ``bundle_id`` means, and how to prove it.

Split out of ``apps.analysis_waveform.score`` (Codex P1 BLOCKING, PR #1536,
thread on ``score.py:162``, fixed alongside the recompute added here) purely
to keep that module under this repo's per-file line ceiling; nothing about
the split changes behavior. Owned here rather than in
``scripts/build_waveform_bundle.py`` because this is the CONSUMER that has to
VALIDATE an identity, not merely produce one: the builder imports
``compute_bundle_id`` from here (by way of ``apps.analysis_waveform.score``,
which re-exports it) rather than restating it, so a builder and a scorer can
never disagree about what a bundle's identity is derived from.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from apps.shared.hashing import content_hash_bytes


def compute_bundle_id(*, seed: int, sample_size: int, tracks: list[dict[str, str]]) -> str:
    """``sha256:<hex>`` over the truth set's own identity, not its packaging.

    ``tracks`` is ``[{"stable_id": ..., "truth_sha256": ..., "payload_sha256":
    ...}, ...]``. Order is irrelevant (stable ids are sorted before hashing)
    and nothing host- or run-specific about a track - its audio path, its
    byte count, when it was built - enters the id. ``payload_sha256`` (the
    audio content only, tags stripped for mp3 - see
    :func:`apps.shared.hashing.sha256_audio_payload`) DOES enter it, so a
    repaired, relinked or re-encoded track changes the id even when its
    ``stable_id`` and truth tag stay the same, while a tag-only rewrite does
    not. The SAME seed and sample size drawn from the SAME library, with the
    SAME audio underneath, reproduce the SAME id anywhere; a different draw
    under either lever, or a changed recording, changes it. See
    ``scripts/build_waveform_bundle.py``'s module docstring's "Bundle
    identity" section, and :func:`verify_bundle_id_matches_its_own_contents`
    below, which recomputes this from a manifest's own contents rather than
    trusting its ``bundle_id`` field as a bare string.
    """
    identity = {
        "sample_seed": seed,
        "sample_size": sample_size,
        "stable_ids": sorted(t["stable_id"] for t in tracks),
        "truth_sha256": {t["stable_id"]: t["truth_sha256"] for t in tracks},
        "payload_sha256": {t["stable_id"]: t["payload_sha256"] for t in tracks},
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return content_hash_bytes(canonical)


def verify_bundle_id_matches_its_own_contents(bundle: Path, manifest: dict[str, Any]) -> None:
    """Refuse a manifest whose declared ``bundle_id`` does not match its OWN contents.

    ``expected_bundle_id`` (in ``score.verify_bundle``) only compares two
    STRINGS: a truth file edited in place, with ``SHA256SUMS`` regenerated to
    match the edit and this manifest's own ``bundle_id`` field left untouched,
    would pass both the name check and the checksum check while scoring under
    a stale identity (Codex P1 BLOCKING, PR #1536, thread on
    ``score.py:162``). Closing it needs two independent bindings, both
    derived from THIS manifest's own ``tracks`` list: each track's recorded
    ``truth_sha256`` must match the ACTUAL sha256 of the truth JSON bytes on
    disk right now (catches an edited truth file whose manifest entry was not
    updated to match), and recomputing :func:`compute_bundle_id` from those
    same - now proven trustworthy - per-track hashes must reproduce this
    manifest's own declared ``bundle_id`` (catches an edited ``truth_sha256``
    field whose ``bundle_id`` was not updated to match). A tamper that
    rewrites the truth bytes, the per-track hash, AND ``bundle_id`` all in
    lockstep can no longer differ from the id an EXTERNAL round log pinned via
    ``--bundle-id`` without a sha256 second-preimage - which is exactly what
    ``expected_bundle_id`` (called right after this, in ``score.py``) then
    catches.

    Only runs when the manifest carries enough of its own history to
    recompute from (``sample_seed``, ``sample_size``, and a ``bundle_id`` -
    see the caller): a manifest built before that history existed has nothing
    here to check against and keeps the ORIGINAL string-only pin behavior.
    """
    mismatches = [
        f"{track['stable_id']}: manifest says truth_sha256={track['truth_sha256']!r}, the "
        f"truth file on disk actually hashes to {actual!r}"
        for track in manifest["tracks"]
        for actual in [content_hash_bytes((bundle / track["truth_file"]).read_bytes())]
        if actual != track["truth_sha256"]
    ]
    if mismatches:
        raise SystemExit(
            f"{bundle} manifest disagrees with its own truth files:\n  " + "\n  ".join(mismatches)
        )
    recomputed = compute_bundle_id(
        seed=manifest["sample_seed"],
        sample_size=manifest["sample_size"],
        tracks=[
            {
                "stable_id": t["stable_id"],
                "truth_sha256": t["truth_sha256"],
                "payload_sha256": t["payload_sha256"],
            }
            for t in manifest["tracks"]
        ],
    )
    if recomputed != manifest["bundle_id"]:
        raise SystemExit(
            f"{bundle} declares bundle_id {manifest['bundle_id']!r}, but recomputing it "
            "from this manifest's own sample_seed, sample_size and per-track hashes "
            f"gives {recomputed!r} instead: the manifest is internally inconsistent "
            "(Codex P1 BLOCKING, PR #1536, thread on score.py:162)"
        )
