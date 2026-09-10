"""Per-band agreement between OUR tri-band peaks and rekordbox PWV6 truth.

``python -m apps.analysis_waveform.score --bundle data/bench/waveform/v1``

A tiny CLI on purpose. ``apps/analysis_bench/`` (specs/native-analysis-v1.md
section 6) is the shared harness this scorer belongs in, and it is being built
in another lane; until it exists, this module owns the waveform lane's scorer so
NATIVE-06 can be measured rather than asserted. Moving it under the bench later
is a re-home, not a rewrite: everything below takes a bundle directory and
returns numbers.

**What is compared.** rekordbox's PWV6 is a whole-track, fixed-width (1200
column) tri-band preview. Our detail peaks are 150 columns/s, so they are
peak-max reduced to the truth's own width before correlating - the same
reduction the served preview uses, so the number describes the lane the deck
actually draws. Pearson r per band, per track, then the median and mean across
tracks.

**What r can and cannot say.** r is scale- and offset-free, so it measures
whether the two envelopes rise and fall together, NOT whether our band levels
match rekordbox's byte values. That is the right question here: the client
normalizes every band by its own p99 before drawing (`render.ts`), so absolute
level is not what a viewer sees. It also means a high r is not a claim that the
crossovers match rekordbox's - only that the content each band tracks does.

**Controls, both of which can fail** (spec section 6):

* positive - truth against itself must score exactly 1.0. If it does not, the
  correlation code is broken and no other number in the table means anything.
* negative - truth against our own bands REVERSED IN TIME, summarized as a
  MAGNITUDE. Same values, same distribution, wrong alignment. A pipeline that
  correlated arrays with themselves, or that compared summary statistics
  instead of envelopes, would score this near 1.0 and give itself away; one
  that correlated a signal with its own negation would score -1.0, which is
  exactly as broken, so the bar is on |r| rather than on the signed value.

Every figure prints with its denominator: tracks scored, and the reasons any
sampled track was not scored (missing audio, changed audio, failed decode, or a
band with no variance, whose correlation is undefined rather than bad). A track
that cannot be measured is reported as UNMEASURED, never as a zero, and never
counted into a denominator no figure was measured over.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from apps.analysis_waveform import decode
from apps.analysis_waveform.bands import _downsample_max
from apps.analysis_waveform.bundle_identity import (
    compute_bundle_id,
    verify_bundle_id_matches_its_own_contents,
)

# compute_bundle_id is a deliberate re-export (bundle_identity.py owns it; this module
# calls verify_bundle_id_matches_its_own_contents, not compute_bundle_id, directly) so
# scripts/build_waveform_bundle.py and existing callers keep reading `score.compute_bundle_id`.
__all__ = ["compute_bundle_id"]

SCORER_VERSION: str = "1.0.0"
# Bundle identity, owned by the CONSUMER that has to validate it:
# scripts/build_waveform_bundle.py imports these rather than restating them, so
# a builder and a scorer can never disagree about which bundle is which.
LANE: str = "waveform"
BUNDLE_VERSION: str = "v1"
EXPECTED_BUNDLE: str = f"{LANE}/{BUNDLE_VERSION}"
CHECKSUM_FILE: str = "SHA256SUMS"
# Any `waveform/<version>` directory is a scorable bundle NAME (Codex P2, PR
# #1536): a `--version` bump lands in its own directory precisely so a bundle
# built under a new sample or seed does not have to overwrite `v1` (see
# scripts/build_waveform_bundle.py's `_refuse_stale_overwrite`), and its
# identity is pinned by `bundle_id`, not by which version string it carries.
# What the name check still has to catch is a bundle from the WRONG LANE
# entirely - a `beatgrid/v1` bundle handed to the waveform scorer.
_BUNDLE_NAME_RE: re.Pattern[str] = re.compile(rf"^{re.escape(LANE)}/[^/]+$")

# Control bars. The positive control is an identity correlation, so anything but
# 1.0 to floating-point tolerance means the correlation code itself is wrong.
# The negative bar is set between what a working pipeline produces (0.06-0.14
# medians on round 0) and what a broken one produces (a pipeline correlating an
# array with itself scores 1.0), deliberately far from both so it is a tripwire
# rather than a threshold anyone has to tune.
# The negative bar is on MAGNITUDE, not on the signed value. A pipeline that
# correlated a signal with its own negation scores -1.0, which is exactly as
# broken as +1.0 and would sail under a signed bar; signed per-track values can
# also cancel to a healthy-looking median across tracks. Measured on round 0's
# 50 tracks, the |r| medians are 0.161 / 0.153 / 0.139, so 0.50 stays a
# tripwire between working and broken rather than a threshold to tune.
POSITIVE_CONTROL_TOLERANCE: float = 1e-9
NEGATIVE_CONTROL_MAX: float = 0.50
# Bundle identity (compute_bundle_id, verify_bundle_id_matches_its_own_contents) lives
# in apps.analysis_waveform.bundle_identity, imported above and re-exported by name so
# scripts/build_waveform_bundle.py and existing callers keep reading `score.compute_bundle_id`.


def _listed_checksums(bundle: Path) -> dict[str, str]:
    """``{relative path: sha256}`` from the bundle's own ``SHA256SUMS``."""
    sums = bundle / CHECKSUM_FILE
    if not sums.is_file():
        raise SystemExit(f"no {CHECKSUM_FILE} in {bundle}; rebuild the bundle")
    listed: dict[str, str] = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        if not name:
            raise SystemExit(f"unparseable {CHECKSUM_FILE} line in {bundle}: {line!r}")
        listed[name] = digest
    return listed


def _bundle_problems(bundle: Path, listed: dict[str, str]) -> list[str]:
    """Every way the files on disk disagree with the checksum list, named."""
    from apps.shared.hashing import sha256_file

    on_disk = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() and path.name != CHECKSUM_FILE
    }
    problems = [f"listed but missing: {name}" for name in sorted(listed) if name not in on_disk]
    problems += [f"present but unlisted: {name}" for name in sorted(on_disk - set(listed))]
    for name, digest in sorted(listed.items()):
        if name not in on_disk:
            continue
        actual = sha256_file(bundle / name).removeprefix("sha256:")
        if actual != digest:
            problems.append(f"checksum mismatch: {name} is {actual}, {CHECKSUM_FILE} says {digest}")
    return problems


def verify_bundle(bundle: Path, *, expected_bundle_id: str | None = None) -> None:
    """Refuse to score a bundle that is not the expected one, INTACT.

    A bundle travels between this Mac and nucbox/agentbox, so it can arrive
    partially copied or hand-edited, and a scorer that reads whatever it finds
    publishes plausible correlations against truth nobody can identify. Three
    checks matter: the name says WHICH LANE (any ``waveform/<version>``
    directory is scorable - a ``--version`` bump is meant to land beside
    ``v1``, not replace it, and ``bundle_id`` below is what actually pins the
    exact truth set, not the version string), the checksums say the bytes are
    the ones its manifest was written against, and ``bundle_id`` (when
    pinned) says it is the SPECIFIC truth set a round named. Every listed file
    is re-hashed, and every file present but UNLISTED is an error too - an
    extra truth file is exactly what a half-finished copy leaves behind.

    ``expected_bundle_id`` closes a gap the name alone cannot: a bundle can
    still be self-consistent (right lane, checksums match) while its
    ``--sample-size``, ``--seed``, or the library it was drawn from differ
    from the round that built it (Codex P1 BLOCKING, PR #1536 - see
    ``scripts/build_waveform_bundle.py``'s "Bundle identity" section). Pass
    the id a round log (or ``BUNDLE_ID``) recorded to refuse a bundle that
    merely LOOKS like the one being asked for. Left ``None`` (the default),
    the STRING pin below is a no-op, so a bundle built before this fix - which
    has no ``bundle_id`` field at all - keeps scoring.

    Before that string comparison runs, a manifest with enough of its own
    history to recompute from (``sample_seed``, ``sample_size``) is checked
    against its OWN contents first - a bare string pin never noticed a
    manifest whose declared id disagreed with what it actually computes to
    (Codex P1 BLOCKING, thread on ``score.py:162``).
    """
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    bundle_name = manifest.get("bundle")
    if not isinstance(bundle_name, str) or not _BUNDLE_NAME_RE.match(bundle_name):
        raise SystemExit(
            f"{bundle} declares bundle {bundle_name!r}, "
            f"this scorer reads {LANE!r}/<version> bundles"
        )
    if (
        manifest.get("bundle_id") is not None
        and "sample_seed" in manifest
        and "sample_size" in manifest
    ):
        verify_bundle_id_matches_its_own_contents(bundle, manifest)
    if expected_bundle_id is not None:
        actual_id = manifest.get("bundle_id")
        if actual_id != expected_bundle_id:
            raise SystemExit(
                f"{bundle} has bundle_id {actual_id!r}, the caller pinned "
                f"{expected_bundle_id!r}: this bundle's manifest is not the truth set "
                "that id names - a different --sample-size, --seed, or a rebuilt "
                "library produced a bundle under the same waveform/v1 name "
                "(Codex P1, PR #1536). Rebuild the pinned id, or repoint --bundle-id "
                "at this bundle's own BUNDLE_ID file"
            )
    problems = _bundle_problems(bundle, _listed_checksums(bundle))
    if problems:
        raise SystemExit(
            f"{bundle} failed verification against its own {CHECKSUM_FILE}:\n  "
            + "\n  ".join(problems)
        )


def load_manifest(bundle: Path) -> dict[str, Any]:
    path = bundle / "manifest.json"
    if not path.is_file():
        raise SystemExit(f"no bundle manifest at {path}; build it first")
    return json.loads(path.read_text(encoding="utf-8"))


def control_failures(report: dict[str, Any]) -> list[str]:
    """Reasons this run may NOT be published, or an empty list.

    Printing a control is not checking one. A run whose negative control scores
    1.0 has a broken correlation and every other figure in it is meaningless; a
    run that measured nothing has no figures at all. Both have to make the
    process exit nonzero, or the controls the module docstring calls
    "able to fail" cannot reject anything.
    """
    failures: list[str] = []
    scored = report["denominators"]["scored"]
    if scored == 0:
        failures.append("0 tracks scored: there is no measurement here to publish")
    for name in decode.BAND_NAMES:
        band_n = report["r"][name]["n"]
        if band_n != scored:
            failures.append(
                f"band {name} has n={band_n} against {scored} scored tracks: a headline "
                "denominator larger than the one a figure was measured over"
            )
    for name in decode.BAND_NAMES:
        positive = report["control_positive"][name]["median"]
        if not np.isfinite(positive) or abs(positive - 1.0) > POSITIVE_CONTROL_TOLERANCE:
            failures.append(
                f"positive control for {name} is {positive}, not 1.0: "
                "the correlation itself is wrong"
            )
        # control_negative is summarized over |r| (see run()), so this reads a
        # magnitude and a -1.0 pipeline cannot pass by being negative enough.
        negative = report["control_negative"][name]["median"]
        if not np.isfinite(negative) or negative > NEGATIVE_CONTROL_MAX:
            failures.append(
                f"negative control for {name} is |{negative}|, above {NEGATIVE_CONTROL_MAX}: "
                "time-reversed bands should not correlate in either direction"
            )
    return failures


def load_truth(bundle: Path, track: dict[str, Any]) -> np.ndarray:
    """``(n, 3)`` truth in 0..1, from the bundle's own JSON (never the library)."""
    payload = json.loads((bundle / track["truth_file"]).read_text(encoding="utf-8"))
    bands = [
        np.asarray(payload["bands"][name], dtype=np.float64) for name in decode.BAND_NAMES
    ]
    return np.clip(np.stack(bands, axis=1) / float(payload["scale"]), 0.0, 1.0)


def pearson(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson r, or NaN when either side has no variance to correlate.

    A constant array has an undefined correlation. Returning NaN keeps it out of
    the mean rather than contributing a 0.0 that would read as "measured, and
    they disagree".
    """
    if a.shape != b.shape or a.size < 2:
        return float("nan")
    a_centered = a - a.mean()
    b_centered = b - b.mean()
    denominator = float(np.sqrt((a_centered**2).sum() * (b_centered**2).sum()))
    if denominator == 0.0:
        return float("nan")
    return float((a_centered * b_centered).sum() / denominator)


def align_to_truth(peaks: np.ndarray, truth_columns: int) -> np.ndarray:
    """Our uint8 ``(n, 3)`` detail peaks, peak-max reduced to the truth's width."""
    return _downsample_max(peaks.astype(np.float64) / 255.0, truth_columns)


def resolve_audio_root(manifest: dict[str, Any], audio_root: Path | None) -> Path | None:
    """The directory every track's relative ``audio_path`` hydrates against.

    ``--audio-root`` overrides the manifest's own recorded root, so a host
    with the library at a different mount point (nucbox, agentbox) can score
    without rebuilding (Codex P1 BLOCKING, PR #1536: the builder used to
    write the Mac's own absolute path, so a bundle could not score anywhere
    else). ``None`` is valid for a bundle built before this fix, whose
    ``audio_path`` values are already absolute and need no root. Fails loud,
    naming the resolved path, when a root IS given but is not a real
    directory - a silently-accepted bad root would read as every track
    missing, indistinguishable from a partial library.
    """
    root = audio_root if audio_root is not None else manifest.get("audio_root")
    if root is None:
        return None
    resolved = Path(root).expanduser().resolve()
    if not resolved.is_dir():
        raise SystemExit(
            f"--audio-root {resolved} is not a directory; cannot hydrate any "
            "track's audio_path against it"
        )
    return resolved


def _hydrate_audio_path(track: dict[str, Any], audio_root: Path | None) -> Path:
    """One track's audio location, joining a relative ``audio_path`` against ``audio_root``.

    An absolute ``audio_path`` (pre-fix bundle) is returned as is - its
    presence or absence is reported softly downstream as "audio not on this
    host" (an expected, honest-denominator outcome). A RELATIVE path is
    different: the operator just told this run where the library lives, so a
    file still missing underneath it is a misconfiguration worth stopping on,
    not a figure to quietly drop. Fails loud on the FIRST such track, naming
    its resolved absolute path.
    """
    raw = Path(track["audio_path"])
    if raw.is_absolute():
        return raw
    if audio_root is None:
        raise SystemExit(
            f"track {track['stable_id']!r} has a relative audio_path "
            f"({track['audio_path']!r}) but no --audio-root was given and the "
            "manifest carries none: pass --audio-root, or rebuild the bundle"
        )
    resolved = audio_root / raw
    if not resolved.is_file():
        raise SystemExit(
            f"track {track['stable_id']!r} audio is missing under --audio-root "
            f"{audio_root}: resolved to {resolved}, which does not exist. Move "
            "the root, fix --audio-root, or rebuild the bundle."
        )
    return resolved


def _audio_identity_mismatch(track: dict[str, Any], audio: Path) -> str | None:
    """The reason ``audio`` disagrees with the manifest, or ``None`` when it agrees.

    ``payload_sha256`` (tags stripped for mp3) is the canonical, retag-tolerant
    check. A manifest with no such field at all - the round 0-2 bundle in
    ``specs/native-analysis-v1.md``, which predates even the FIRST
    payload-hashing fix on this PR (Codex P1 BLOCKING, thread on
    ``score.py:358``: canonical fixture inputs must stay reproducible, not
    raise ``KeyError`` on a field never recorded) - falls back to the
    ORIGINAL whole-file ``audio_sha256`` check instead.
    """
    from apps.shared.hashing import sha256_audio_payload, sha256_file

    if "payload_sha256" in track:
        payload_digest = sha256_audio_payload(audio)
        if payload_digest != track["payload_sha256"]:
            return (
                f"audio payload sha256 differs from the bundle ({payload_digest}): "
                "the content changed since the bundle was built, not just tags"
            )
        return None
    digest = sha256_file(audio)
    if digest != track["audio_sha256"]:
        return f"audio sha256 differs from the bundle ({digest})"
    return None


def score_track(
    bundle: Path,
    track: dict[str, Any],
    *,
    profile: decode.DecodeProfile = decode.PROFILE,
    audio_root: Path | None = None,
) -> dict[str, Any]:
    """One track's per-band r, or an UNMEASURED row naming why."""
    audio = _hydrate_audio_path(track, audio_root)
    if not audio.is_file():
        return {"stable_id": track["stable_id"], "unmeasured": "audio not on this host"}
    mismatch = _audio_identity_mismatch(track, audio)
    if mismatch is not None:
        return {"stable_id": track["stable_id"], "unmeasured": mismatch}
    truth = load_truth(bundle, track)
    started = time.monotonic()
    try:
        peaks = decode.decode_peaks(audio, profile)
    except decode.LocalDecodeUnavailable as exc:
        return {"stable_id": track["stable_id"], "unmeasured": f"not_decoded: {exc.reason}"}
    elapsed_s = time.monotonic() - started
    if peaks.shape[0] < truth.shape[0]:
        return {
            "stable_id": track["stable_id"],
            "unmeasured": (
                f"{peaks.shape[0]} decoded columns is fewer than the truth's "
                f"{truth.shape[0]}; nothing to reduce"
            ),
        }
    ours = align_to_truth(peaks, truth.shape[0])
    metrics = {
        "r": {
            name: pearson(ours[:, index], truth[:, index])
            for index, name in enumerate(decode.BAND_NAMES)
        },
        "control_positive": {
            name: pearson(truth[:, index], truth[:, index])
            for index, name in enumerate(decode.BAND_NAMES)
        },
        # Magnitude, so a sign flip cannot look like disagreement and cannot
        # cancel against another track when these are summarized.
        "control_negative": {
            name: abs(pearson(ours[::-1, index], truth[:, index]))
            for index, name in enumerate(decode.BAND_NAMES)
        },
    }
    undefined = [
        f"{group}.{band}"
        for group, values in metrics.items()
        for band, value in values.items()
        if not np.isfinite(value)
    ]
    if undefined:
        # A band with no variance has an UNDEFINED correlation, not a bad one.
        # Counting this track in `scored` while the per-band summaries quietly
        # drop it would publish a headline denominator larger than the one any
        # figure was actually measured over (HONEST DENOMINATORS).
        return {
            "stable_id": track["stable_id"],
            "unmeasured": f"undefined correlation (no variance) for {', '.join(undefined)}",
        }
    return {
        "stable_id": track["stable_id"],
        "decode_s": elapsed_s,
        "audio_s": peaks.shape[0] / profile.detail_columns_per_s,
        **metrics,
    }


def _summary(values: list[float]) -> dict[str, float]:
    finite = [v for v in values if np.isfinite(v)]
    if not finite:
        return {"n": 0, "mean": float("nan"), "median": float("nan"), "min": float("nan")}
    array = np.asarray(finite)
    return {
        "n": len(finite),
        "mean": float(array.mean()),
        "median": float(np.median(array)),
        "min": float(array.min()),
    }


def _metric_summaries(scored: list[dict[str, Any]]) -> dict[str, Any]:
    """Across-track summaries for every per-track metric a scored row carries."""
    per_band = {
        group: {
            name: _summary([row[group][name] for row in scored])
            for name in decode.BAND_NAMES
        }
        for group in ("r", "control_positive", "control_negative")
    }
    return {
        **per_band,
        "decode_s": _summary([row["decode_s"] for row in scored]),
        "realtime_factor": _summary(
            [row["audio_s"] / row["decode_s"] for row in scored if row["decode_s"] > 0]
        ),
    }


def run(
    bundle: Path,
    *,
    limit: int | None = None,
    profile: decode.DecodeProfile = decode.PROFILE,
    expected_bundle_id: str | None = None,
    audio_root: Path | None = None,
) -> dict[str, Any]:
    verify_bundle(bundle, expected_bundle_id=expected_bundle_id)
    manifest = load_manifest(bundle)
    root = resolve_audio_root(manifest, audio_root)
    tracks = manifest["tracks"][: limit or len(manifest["tracks"])]
    rows = [score_track(bundle, track, profile=profile, audio_root=root) for track in tracks]
    scored = [row for row in rows if "r" in row]
    unmeasured = [row for row in rows if "unmeasured" in row]

    report: dict[str, Any] = {
        "scorer_version": SCORER_VERSION,
        "bundle": manifest["bundle"],
        "bundle_id": manifest.get("bundle_id"),
        "truth_tag": manifest["truth_tag"],
        "producer": {
            "filter_graph": decode.band_filter_graph(profile),
            "profile": profile.identity(),
            "sample_rate_hz": profile.sample_rate_hz,
            "crossover_low_hz": profile.crossover_low_hz,
            "crossover_high_hz": profile.crossover_high_hz,
            "filter_sections": profile.filter_sections,
        },
        "denominators": {
            "in_bundle": len(manifest["tracks"]),
            "attempted": len(rows),
            "scored": len(scored),
            "unmeasured": len(unmeasured),
        },
        **_metric_summaries(scored),
        "unmeasured_reasons": [
            f"{row['stable_id']}: {row['unmeasured']}" for row in unmeasured
        ],
        "tracks": rows,
    }
    return report


def _print(report: dict[str, Any]) -> None:
    counts = report["denominators"]
    print(
        f"scorer {report['scorer_version']} on bundle {report['bundle']} "
        f"(id {report.get('bundle_id')})"
    )
    print(f"graph: {report['producer']['filter_graph']}")
    print(
        f"denominator: {counts['scored']} scored of {counts['attempted']} attempted "
        f"({counts['in_bundle']} in bundle, {counts['unmeasured']} unmeasured)"
    )
    if counts["scored"] == 0:
        print("NOTHING WAS MEASURED - every figure below is empty, not zero")
    header = f"{'band':<6}{'median r':>10}{'mean r':>10}{'min r':>10}{'n':>5}"
    print(header)
    for name in decode.BAND_NAMES:
        s = report["r"][name]
        print(f"{name:<6}{s['median']:>10.3f}{s['mean']:>10.3f}{s['min']:>10.3f}{s['n']:>5}")
    for label in ("control_positive", "control_negative"):
        values = ", ".join(
            f"{name} {report[label][name]['median']:.3f}" for name in decode.BAND_NAMES
        )
        print(f"{label}: {values}")
    decode_s = report["decode_s"]
    realtime = report["realtime_factor"]
    slowest = max(
        (row["decode_s"] for row in report["tracks"] if "decode_s" in row),
        default=float("nan"),
    )
    print(
        f"decode: median {decode_s['median']:.2f}s per track, "
        f"mean {decode_s['mean']:.2f}s, slowest {slowest:.2f}s; "
        f"median {realtime['median']:.1f}x realtime"
    )
    print(
        "decode times are wall clock on whatever else this host was running; "
        "for an attributable timing use a paired, interleaved run"
    )
    for reason in report["unmeasured_reasons"]:
        print(f"UNMEASURED {reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apps.analysis_waveform.score")
    parser.add_argument(
        "--bundle",
        type=Path,
        required=True,
        help="fixture bundle directory (scripts/build_waveform_bundle.py builds it)",
    )
    parser.add_argument("--limit", type=int, default=None, help="score only the first N tracks")
    parser.add_argument("--json", type=Path, default=None, help="also write the full report here")
    parser.add_argument(
        "--bundle-id",
        type=str,
        default=None,
        help=(
            "expected bundle_id (from a round log or the bundle's own BUNDLE_ID file); "
            "refuses to score a manifest whose own bundle_id differs"
        ),
    )
    parser.add_argument(
        "--audio-root",
        type=Path,
        default=None,
        help=(
            "directory this host's copy of the sampled library lives under; overrides "
            "the manifest's own recorded audio_root, so a bundle built on one host can "
            "score on another (nucbox, agentbox) without a rebuild. Defaults to the "
            "manifest's audio_root; a bundle built before this fix carries none and "
            "needs no root, its audio_path values are already absolute"
        ),
    )
    args = parser.parse_args(argv)
    report = run(
        args.bundle,
        limit=args.limit,
        expected_bundle_id=args.bundle_id,
        audio_root=args.audio_root,
    )
    _print(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    failures = control_failures(report)
    for failure in failures:
        print(f"CONTROL FAILED: {failure}")
    if failures:
        print("this run's figures are NOT publishable; nothing above may be quoted")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
