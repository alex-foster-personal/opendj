"""The scorer's controls and its bundle check can REJECT, through the real CLI.

Three review findings shaped this file. Sol's: a control that is printed is not
a control, and a bundle that is parsed is not a bundle that was verified.
Codex's: tests that hand ``control_failures()`` a manufactured report stay green
if ``main()`` stops calling the gate, so they never established the CLI
rejection they were offered as evidence for.

So the subject here is ``score.main``, driven over a REAL bundle built from a
real WAV through the production decoder, with real per-file sha256s and a real
SHA256SUMS. Nothing is stubbed and no report is fabricated: the failure cases
mutate a DISPOSABLE COPY of a report the production ``run()`` actually produced,
which is the one mutation shape AGENTS.md permits ("Tests that need mutations
operate on disposable hydrated copies; the source fixture must remain
byte-identical").

Every guard is paired: one arm proving it passes on a good input, one proving it
goes red on a bad one. A guard only ever tested against the good case is a guard
nobody has seen bite.

  - [if] a run with 0 scored or inverted controls still exits 0 [then] fail, [else stop].
  - [if] a bundle's bytes or version differ and scoring proceeds [then] fail, [else stop].
  - [if] a manifest's truth file is edited and checksums regenerated [then] refuse it, [else stop].
  - [if] truth_sha256 is updated but bundle_id is not recomputed [then] refuse it, [else stop].
  - [if] a legacy manifest has no payload_sha256 [then] audio_sha256 still gates it, [else stop].
  - [if] that legacy fallback's audio genuinely changed [then] still refuse it, [else stop].
"""

from __future__ import annotations

import copy
import json
import math
import struct
import wave
from pathlib import Path
from random import Random

import pytest

from apps.analysis_waveform import decode, score
from apps.analysis_waveform.bands import _downsample_max
from apps.shared.hashing import sha256_audio_payload, sha256_file

pytestmark = [pytest.mark.requirement("NATIVE-06"), pytest.mark.requires_ffmpeg]

SAMPLE_RATE_HZ = 44_100
# Long enough that the decode yields more columns than the 1200-column truth it
# is reduced to; under that, `run()` reports the track UNMEASURED and there is
# nothing for the control gate to act on.
CLIP_S = 12.0
TRUTH_COLUMNS = 1200
TRUTH_SCALE = 127.0


def _write_three_band_clip(path: Path) -> None:
    """A real WAV with content in ALL THREE bands and a TIME-ASYMMETRIC envelope.

    Three properties, all load-bearing. Content in every band keeps each band's
    correlation defined - a constant band has an UNDEFINED r, and `run()` would
    correctly report the track unmeasured, leaving nothing to gate on. A moving
    envelope is what gives each band variance to correlate at all. And the
    envelope must not be self-similar under time reversal, or the scorer's own
    negative control fires on the fixture: a first draft used a slow |sin|
    envelope and the reversed correlation came back at 0.65, which the gate
    (correctly) refused. A seeded per-block amplitude sequence has no such
    symmetry and is still exactly reproducible.
    """
    blocks = Random(20260908)
    block_samples = SAMPLE_RATE_HZ // 10
    frames = bytearray()
    envelope = 0.0
    for i in range(int(SAMPLE_RATE_HZ * CLIP_S)):
        if i % block_samples == 0:
            envelope = 0.15 + 0.8 * blocks.random()
        t = i / SAMPLE_RATE_HZ
        value = envelope * (
            0.5 * math.sin(2 * math.pi * 60.0 * t)
            + 0.3 * math.sin(2 * math.pi * 1_000.0 * t)
            + 0.2 * math.sin(2 * math.pi * 12_000.0 * t)
        )
        frames += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32000))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE_HZ)
        handle.writeframes(bytes(frames))


def _write_checksums(bundle: Path) -> None:
    lines = [
        f"{sha256_file(path).removeprefix('sha256:')}  {path.relative_to(bundle).as_posix()}"
        for path in sorted(bundle.rglob("*"))
        if path.is_file() and path.name != score.CHECKSUM_FILE
    ]
    (bundle / score.CHECKSUM_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_bundle(
    root: Path,
    *,
    bundle_name: str | None = None,
    bundle_id: str | None = None,
    include_payload_hash: bool = True,
) -> Path:
    """A real one-track bundle: real audio, real decode, real truth, real sums.

    The truth is this repo's OWN decode of the clip, quantized to the rekordbox
    PWV6 convention (1200 columns, 0..127), so the correlation is high by
    construction. That is what a passing arm needs; it is not a claim about
    rekordbox, and no figure from this bundle is quoted anywhere.

    ``bundle_id`` is omitted from the manifest unless given, matching a bundle
    built before the identity fix (PR #1536); tests that pin an id pass one.
    ``include_payload_hash=False`` reproduces the round 0-2 bundle recorded in
    specs/native-analysis-v1.md, which predates payload_sha256 entirely
    (Codex P1 BLOCKING, PR #1536, thread on score.py:358).
    """
    bundle = root / "v1"
    (bundle / "truth").mkdir(parents=True, exist_ok=True)
    audio = root / "clip.wav"
    _write_three_band_clip(audio)

    reduced = _downsample_max(decode.decode_peaks(audio), TRUTH_COLUMNS)
    (bundle / "truth" / "clip.json").write_text(
        json.dumps(
            {
                "stable_id": "clip",
                "tag": "PWV6",
                "scale": TRUTH_SCALE,
                "columns": TRUTH_COLUMNS,
                "bands": {
                    name: [round(float(v) / 255.0 * TRUTH_SCALE) for v in reduced[:, index]]
                    for index, name in enumerate(decode.BAND_NAMES)
                },
            }
        ),
        encoding="utf-8",
    )
    track_payload: dict = {
        "stable_id": "clip",
        "truth_file": "truth/clip.json",
        "audio_path": str(audio),
        "audio_sha256": sha256_file(audio),
    }
    if include_payload_hash:
        track_payload["payload_sha256"] = sha256_audio_payload(audio)
    manifest_payload: dict = {
        "bundle": bundle_name or score.EXPECTED_BUNDLE,
        "truth_tag": "PWV6",
        "tracks": [track_payload],
    }
    if bundle_id is not None:
        manifest_payload["bundle_id"] = bundle_id
    (bundle / "manifest.json").write_text(json.dumps(manifest_payload), encoding="utf-8")
    _write_checksums(bundle)
    return bundle


def _build_full_identity_bundle(root: Path) -> Path:
    """Same one-track construction as ``_build_bundle``, but with the FULL
    identity fields a REAL bundle always carries together (``sample_seed``,
    ``sample_size``, and a ``bundle_id`` correctly computed from the
    manifest's own per-track hashes) - what
    ``_verify_bundle_id_matches_its_own_contents`` needs present before it has
    anything to recompute against."""
    from apps.shared.hashing import content_hash_bytes

    bundle = root / "v1"
    (bundle / "truth").mkdir(parents=True, exist_ok=True)
    audio = root / "clip.wav"
    _write_three_band_clip(audio)

    reduced = _downsample_max(decode.decode_peaks(audio), TRUTH_COLUMNS)
    truth_bytes = json.dumps(
        {
            "stable_id": "clip",
            "tag": "PWV6",
            "scale": TRUTH_SCALE,
            "columns": TRUTH_COLUMNS,
            "bands": {
                name: [round(float(v) / 255.0 * TRUTH_SCALE) for v in reduced[:, index]]
                for index, name in enumerate(decode.BAND_NAMES)
            },
        }
    ).encode("utf-8")
    (bundle / "truth" / "clip.json").write_bytes(truth_bytes)

    truth_sha256 = content_hash_bytes(truth_bytes)
    payload_sha256 = sha256_audio_payload(audio)
    bundle_id = score.compute_bundle_id(
        seed=1,
        sample_size=1,
        tracks=[
            {"stable_id": "clip", "truth_sha256": truth_sha256, "payload_sha256": payload_sha256}
        ],
    )
    manifest_payload = {
        "bundle": score.EXPECTED_BUNDLE,
        "bundle_id": bundle_id,
        "truth_tag": "PWV6",
        "sample_seed": 1,
        "sample_size": 1,
        "tracks": [
            {
                "stable_id": "clip",
                "truth_file": "truth/clip.json",
                "truth_sha256": truth_sha256,
                "audio_path": str(audio),
                "audio_sha256": sha256_file(audio),
                "payload_sha256": payload_sha256,
            }
        ],
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest_payload), encoding="utf-8")
    _write_checksums(bundle)
    return bundle


@pytest.fixture(scope="module")
def real_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build_bundle(tmp_path_factory.mktemp("waveform-bundle"))


@pytest.fixture(scope="module")
def real_report(real_bundle: Path) -> dict:
    """A report the PRODUCTION `run()` actually produced. Never edited in place."""
    return score.run(real_bundle)


# ----- the real CLI entry point -------------------------------------------------


def test_the_cli_exits_zero_on_a_healthy_bundle(real_bundle: Path) -> None:
    """The positive arm, through `main` rather than around it: without it, a
    scorer that rejected everything would satisfy every rejection test below."""
    assert score.main(["--bundle", str(real_bundle)]) == 0


def test_the_cli_exits_nonzero_when_nothing_could_be_measured(tmp_path: Path) -> None:
    """The rejection Codex asked to see proven at the entry point: the audio is
    genuinely absent, so `run()` scores nothing and `main` must not return 0."""
    bundle = _build_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    Path(manifest["tracks"][0]["audio_path"]).unlink()
    assert score.main(["--bundle", str(bundle)]) == 2


def test_the_cli_refuses_a_corrupted_bundle_before_it_decodes_anything(
    tmp_path: Path,
) -> None:
    bundle = _build_bundle(tmp_path)
    truth = bundle / "truth" / "clip.json"
    truth.write_text(
        truth.read_text(encoding="utf-8").replace('"scale": 127.0', '"scale": 63.0'),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="checksum mismatch"):
        score.main(["--bundle", str(bundle)])


# ----- the control gate, over a report the real run produced --------------------


def test_a_real_run_passes_its_own_controls(real_report: dict) -> None:
    assert score.control_failures(real_report) == []
    assert real_report["denominators"]["scored"] == 1


def test_an_inverted_negative_control_is_refused(real_report: dict) -> None:
    """A pipeline correlating an array with itself scores the time-reversed
    control at 1.0. Mutated on a DISPOSABLE COPY; the source stays byte-identical."""
    report = copy.deepcopy(real_report)
    report["control_negative"]["mid"] = {"median": 1.0, "n": 1}
    assert any("negative control for mid" in f for f in score.control_failures(report))
    assert score.control_failures(real_report) == [], "the source report must be unchanged"


def test_a_sign_flipped_negative_control_is_refused(real_report: dict) -> None:
    """The overshoot direction. A signed bar of `> 0.50` passes -1.0, which is
    exactly as broken as +1.0; the summary is a magnitude for that reason."""
    report = copy.deepcopy(real_report)
    report["control_negative"]["low"] = {"median": 0.99, "n": 1}
    assert score.control_failures(report)


def test_a_broken_positive_control_is_refused(real_report: dict) -> None:
    report = copy.deepcopy(real_report)
    report["control_positive"]["low"] = {"median": 0.97, "n": 1}
    assert any("positive control for low" in f for f in score.control_failures(report))


def test_a_nan_control_is_refused_rather_than_read_as_passing(real_report: dict) -> None:
    """NaN compares false against every bound, so a naive `> MAX` test lets an
    unmeasurable control through as if it had passed."""
    report = copy.deepcopy(real_report)
    report["control_negative"]["high"] = {"median": float("nan"), "n": 1}
    assert score.control_failures(report)


def test_a_band_measured_over_fewer_tracks_than_the_headline_is_refused(
    real_report: dict,
) -> None:
    """HONEST DENOMINATORS. If a band's own n is below the scored count, the
    headline denominator is larger than the one that figure came from."""
    report = copy.deepcopy(real_report)
    report["denominators"]["scored"] = 2
    assert any("against 2 scored" in f for f in score.control_failures(report))


# ----- bundle verification, on disposable copies of a real bundle ---------------


def test_an_intact_bundle_verifies(real_bundle: Path) -> None:
    score.verify_bundle(real_bundle)


def test_a_half_copied_bundle_is_refused(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path)
    (bundle / "truth" / "clip.json").unlink()
    with pytest.raises(SystemExit, match="listed but missing"):
        score.verify_bundle(bundle)


def test_an_unlisted_extra_file_is_refused(tmp_path: Path) -> None:
    """The direction a checksum list alone does not cover: every listed file can
    hash correctly while a stray file sits beside them."""
    bundle = _build_bundle(tmp_path)
    (bundle / "truth" / "stray.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="present but unlisted"):
        score.verify_bundle(bundle)


def test_a_bundle_of_another_version_under_the_same_lane_is_accepted_by_name(
    tmp_path: Path,
) -> None:
    """A `--version` bump lands beside v1, not over it (Codex P2, PR #1536):
    the scorer's name check is per-LANE, and identity is pinned by
    `bundle_id`, not by which version string a bundle happens to carry."""
    bundle = _build_bundle(tmp_path, bundle_name="waveform/v99")
    score.verify_bundle(bundle)


def test_a_bundle_from_the_wrong_lane_is_refused(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path, bundle_name="beatgrid/v1")
    with pytest.raises(SystemExit, match="this scorer reads"):
        score.verify_bundle(bundle)


def test_a_malformed_bundle_name_is_refused(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path, bundle_name="waveformv1")
    with pytest.raises(SystemExit, match="this scorer reads"):
        score.verify_bundle(bundle)


def test_a_bundle_with_no_checksums_is_refused(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path)
    (bundle / score.CHECKSUM_FILE).unlink()
    with pytest.raises(SystemExit, match=score.CHECKSUM_FILE):
        score.verify_bundle(bundle)


# ----- bundle_id pinning (Codex P1 BLOCKING, PR #1536) --------------------
#
# The bundle NAME check above (test_a_bundle_of_another_version_is_refused)
# catches a bundle declaring the wrong version. It does not catch a bundle
# that still says "waveform/v1" but was built from a different --sample-size,
# --seed, or library snapshot - exactly the gap the P1 named. bundle_id closes
# it: a caller that pins an id (from a round log or --bundle-id) gets refused
# the moment the manifest's own id disagrees, before SHA256SUMS is even read.


def test_a_pinned_bundle_id_matching_the_manifest_passes(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path, bundle_id="sha256:abc123")
    score.verify_bundle(bundle, expected_bundle_id="sha256:abc123")


def test_a_pinned_bundle_id_not_matching_the_manifest_is_refused(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path, bundle_id="sha256:abc123")
    with pytest.raises(SystemExit, match="bundle_id"):
        score.verify_bundle(bundle, expected_bundle_id="sha256:different")


def test_an_unpinned_run_is_unaffected_by_bundle_id(real_bundle: Path) -> None:
    """No --bundle-id given (the default): the identity check is a no-op, so
    every existing bundle from before this fix keeps scoring."""
    score.verify_bundle(real_bundle, expected_bundle_id=None)


def test_the_cli_refuses_via_bundle_id_flag_when_pinned_id_mismatches(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path, bundle_id="sha256:abc123")
    with pytest.raises(SystemExit, match="bundle_id"):
        score.main(["--bundle", str(bundle), "--bundle-id", "sha256:different"])


def test_the_cli_exits_zero_via_bundle_id_flag_when_pinned_id_matches(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path, bundle_id="sha256:abc123")
    assert score.main(["--bundle", str(bundle), "--bundle-id", "sha256:abc123"]) == 0


# ----- legacy manifests predating payload_sha256 stay scorable -------------------
# Codex P1 BLOCKING (PR #1536, thread on score.py:358): the round 0-2 bundle
# recorded in specs/native-analysis-v1.md predates payload_sha256 entirely, so
# directly indexing track["payload_sha256"] raised KeyError instead of scoring it,
# breaking reproducibility of an already-recorded, immutable fixture.


def test_a_legacy_manifest_without_payload_sha256_still_scores(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path, include_payload_hash=False)
    report = score.run(bundle)
    assert report["denominators"]["scored"] == 1, report["unmeasured_reasons"]


def test_a_legacy_manifest_still_refuses_a_real_audio_sha256_mismatch(tmp_path: Path) -> None:
    """The fallback is real content-checking, not a bypass: a legacy manifest
    with no payload_sha256 must still catch audio that genuinely changed,
    via the ORIGINAL whole-file audio_sha256 it does carry."""
    bundle = _build_bundle(tmp_path, include_payload_hash=False)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tracks"][0]["audio_sha256"] = "sha256:" + "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _write_checksums(bundle)

    report = score.run(bundle)
    row = next(r for r in report["tracks"] if r["stable_id"] == "clip")
    assert row.get("unmeasured", "").startswith("audio sha256 differs"), row
    assert report["denominators"]["scored"] == 0


# ----- bundle_id recomputed from its own contents, not just string-compared -----
# Codex P1 BLOCKING (PR #1536, thread on score.py:162): the pin above only ever
# compared two strings, so a manifest whose OWN bundle_id field disagreed with
# what its own recorded truth hashes compute to was never caught.


def test_a_full_identity_bundle_with_a_correct_bundle_id_verifies(tmp_path: Path) -> None:
    """The positive arm: a real, untampered bundle_id-bearing manifest must
    still pass, or the new check is not a check that can also NOT fire."""
    bundle = _build_full_identity_bundle(tmp_path)
    score.verify_bundle(bundle)


def test_a_tampered_truth_file_with_regenerated_checksums_is_still_refused(
    tmp_path: Path,
) -> None:
    """The exact scenario Codex named: edit the truth file, regenerate
    SHA256SUMS to match the edit, leave manifest.json (bundle_id AND the
    per-track truth_sha256 field) untouched. SHA256SUMS alone is
    self-referential and passes; the old string-only bundle_id pin would also
    have passed (bundle_id itself was never touched)."""
    bundle = _build_full_identity_bundle(tmp_path)
    truth_path = bundle / "truth" / "clip.json"
    tampered = json.loads(truth_path.read_text(encoding="utf-8"))
    tampered["bands"]["low"][0] = (tampered["bands"]["low"][0] + 1) % 128
    truth_path.write_text(json.dumps(tampered), encoding="utf-8")
    _write_checksums(bundle)  # self-consistent with the tamper, same as an attacker would leave it
    with pytest.raises(SystemExit, match="disagrees with its own truth files"):
        score.verify_bundle(bundle)


def test_a_tampered_truth_sha256_without_a_matching_bundle_id_is_refused(tmp_path: Path) -> None:
    """A more thorough tamper: the truth file AND its own manifest
    truth_sha256 entry are both updated to match each other, but bundle_id is
    left stale - the first check alone would not catch this, since the
    per-track hash and the file now agree; only recomputing bundle_id from
    that (self-consistent but stale-relative-to-bundle_id) hash does."""
    from apps.shared.hashing import content_hash_bytes

    bundle = _build_full_identity_bundle(tmp_path)
    truth_path = bundle / "truth" / "clip.json"
    tampered = json.loads(truth_path.read_text(encoding="utf-8"))
    tampered["bands"]["low"][0] = (tampered["bands"]["low"][0] + 1) % 128
    tampered_bytes = json.dumps(tampered).encode("utf-8")
    truth_path.write_bytes(tampered_bytes)

    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tracks"][0]["truth_sha256"] = content_hash_bytes(tampered_bytes)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _write_checksums(bundle)

    with pytest.raises(SystemExit, match="internally inconsistent"):
        score.verify_bundle(bundle)


def test_the_builder_and_the_scorer_read_one_bundle_identity() -> None:
    """Two constants named the same thing in two files is how a builder starts
    writing a bundle the scorer no longer recognises."""
    from scripts import build_waveform_bundle

    assert build_waveform_bundle.EXPECTED_BUNDLE is score.EXPECTED_BUNDLE
    assert build_waveform_bundle.CHECKSUM_FILE is score.CHECKSUM_FILE
    assert build_waveform_bundle.compute_bundle_id is score.compute_bundle_id
