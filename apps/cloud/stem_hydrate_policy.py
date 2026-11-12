"""Judge a freshly downloaded stem-set against the STEM-8231 size rule before it enters the store (#9665).

The R2 hydrate fetches whatever the index lists byte for byte, so a legacy
FLAC stem-set of an MP3 source would land as FLAC. This runs on the hydrate's
private verify copy, after the strict load and before the swap, and uses the
verdicts :func:`apps.stems.recompress.classify` already gives every local
bundle (which call the stem_size_policy functions).

* [if] the set breaks the rule (a FLAC set of a lossy source, or an MP3 set
  over the whole-set rule) [then] it is re-encoded to the policy MP3 in the
  verify copy and only that result is published.
* [if] the re-encode cannot run (no MP3 encoder) or fails [then]
  StemPolicyRefusedError with a named reason; the caller leaves nothing in
  the store.
* [if] the set already follows the rule, is lossless by choice, or cannot be
  judged here (no source file, unknown format, duration mismatch) [then] it
  publishes unchanged.

The re-encoded bundle no longer matches the index byte for byte, so the cache
budget will not evict it as R2-held (STEM-39/40): the safe direction, and the
same state ``recompress`` leaves.
"""

from __future__ import annotations

import logging
from pathlib import Path

from apps.cloud.eviction import HydrationError
from apps.stems import recompress
from apps.stems.artifacts import StemArtifactError
from apps.stems.stem_encode import Mp3EncoderUnavailableError

log = logging.getLogger(__name__)

STEM_HYDRATE_POLICY_REFUSED = "STEM_HYDRATE_POLICY_REFUSED"
HYDRATE_REENCODED = "re-encoded to the policy MP3 on arrival"
HYDRATE_REFUSED_NO_MP3_ENCODER = "stem-set breaks the size rule and this install cannot write MP3"
HYDRATE_REFUSED_REENCODE_FAILED = "stem-set breaks the size rule and its re-encode failed"
HYDRATE_REFUSED_UNREADABLE_MANIFEST = "stem-set manifest cannot be judged against the size rule"

_COMPLIANT = frozenset(
    {
        recompress.SKIP_AT_POLICY,
        recompress.SKIP_LOSSLESS_SOURCE,
        recompress.SKIP_DELIBERATE_LOSSLESS,
        recompress.SKIP_ALREADY_LOSSY,
    }
)
_CANNOT_JUDGE = frozenset({recompress.SKIP_NO_SOURCE, recompress.SKIP_UNKNOWN_SOURCE, recompress.SKIP_SOURCE_DURATION})


class StemPolicyRefusedError(HydrationError):
    """A downloaded stem-set breaks the size rule and cannot be brought to it here."""

    code = STEM_HYDRATE_POLICY_REFUSED


def conform_fetched_bundle(container: Path, stable_id: str, *, data_dir: Path) -> str | None:
    """Bring ``container/stable_id`` to the size rule; the outcome note, or None when unchanged."""
    library_path = recompress.library_paths(data_dir, [stable_id]).get(stable_id)
    candidate = recompress.classify(container, stable_id, library_path)
    verdict = candidate.verdict
    if verdict in _COMPLIANT:
        return None
    if verdict in _CANNOT_JUDGE:
        log.info("%s: hydrated unchanged, size rule not judged (%s %s)", stable_id, verdict, candidate.detail)
        return None
    if verdict == recompress.SKIP_INVALID:
        raise StemPolicyRefusedError(f"{HYDRATE_REFUSED_UNREADABLE_MANIFEST}: {candidate.detail}")
    if verdict != recompress.ELIGIBLE:
        raise StemPolicyRefusedError(f"{HYDRATE_REFUSED_UNREADABLE_MANIFEST}: unhandled verdict {verdict}")
    try:
        outcome = recompress.reencode_staged_bundle(candidate)
    except Mp3EncoderUnavailableError as exc:
        raise StemPolicyRefusedError(f"{HYDRATE_REFUSED_NO_MP3_ENCODER}: {exc}") from exc
    except (StemArtifactError, recompress.BundleChangedError, recompress.WorkspaceCleanupError, OSError) as exc:
        raise StemPolicyRefusedError(f"{HYDRATE_REFUSED_REENCODE_FAILED}: {exc}") from exc
    return (
        f"{HYDRATE_REENCODED}: {outcome.media_type_before} -> audio/mpeg, "
        f"{outcome.before_bytes} -> {outcome.after_bytes} bytes"
    )
