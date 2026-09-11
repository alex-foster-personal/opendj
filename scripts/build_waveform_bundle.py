"""Build the waveform fixture bundle: rekordbox PWV6 truth for sampled tracks.

``python -m scripts.build_waveform_bundle build`` writes
``data/bench/<lane>/<version>/`` (gitignored, ``data/*``) holding, per sampled
track, the rekordbox tri-band preview tag as JSON, plus a manifest and a
``SHA256SUMS``. ``apps.analysis_waveform.score`` reads that bundle; nothing else
in the repo may reach into the library to score, so a number produced on this
Mac and a number produced on nucbox are produced from the same truth or not at
all.

What the bundle DOES carry: the truth bands, the audio's path RELATIVE to a
recorded ``audio_root`` (Codex P1 BLOCKING, PR #1536 - an absolute Mac path
baked into the manifest meant a bundle built here could never score on nucbox
or agentbox at all, since that exact path exists on no other host), its sha256
and its size. What it deliberately does NOT carry: the audio. rekordbox PWV6
describes a WHOLE track, so an excerpt bundle (beatbench section 3.4) would
have nothing to align against, and 50 whole tracks is ~500 MB. A host without
a local copy of the library therefore cannot run the waveform scorer from the
bundle alone - it can only prove, by sha256, that the audio it does have is
the same audio; a host WITH a copy at a different mount point points
``apps.analysis_waveform.score`` at it with ``--audio-root`` instead of
rebuilding. That is a stated limitation of THIS bundle, not of the harness.

Sampling is a seeded random draw over every candidate, not the first N: the
library is ordered by import, so a head slice would sample one era of one
collection. The seed is recorded in the manifest so the same draw reproduces.

**Bundle identity** (Codex P1 BLOCKING, PR #1536). ``--sample-size``,
``--seed``, or a library that changed between rebuilds can each produce a
DIFFERENT truth set while still writing a self-consistent bundle named
``waveform/v1`` - the version name alone cannot tell two such bundles apart,
so two rounds could claim "the same fixture" while scoring different tracks.
``bundle_id`` fixes that: it is the sha256 of a canonical JSON payload holding
the seed, the sample size, the sorted stable ids, each track's OWN truth
payload sha256, and each track's OWN AUDIO payload sha256 - tags stripped for
mp3, whole file otherwise (:func:`compute_bundle_id`,
:func:`apps.shared.hashing.sha256_audio_payload`). The audio payload hash is
what makes a repaired, relinked or re-encoded track change the identity even
when its ``stable_id`` and rekordbox row do not: a rekordbox repair or a
relink can leave the truth (PWV6) tag and the sampled track SET identical
while the bytes a listener actually hears differ underneath. It deliberately
excludes anything host- or run-specific - ``built_at_utc``, ``audio_root``,
the (now-relative) ``audio_path``, ``audio_bytes``, and the whole-file
``audio_sha256`` (which a tag-only rewrite would flip for no content reason) -
so the SAME seed and
sample size drawn from the SAME library, with the SAME audio underneath,
reproduce the same id on any host, at any time, while a different draw, or a
changed recording, under any of those levers changes it. The id is written
into ``manifest.json`` and to a sidecar ``BUNDLE_ID`` file, and printed at the
end of a build. ``apps.analysis_waveform.score.verify_bundle`` can be pinned
to an expected id (``--bundle-id``) so a round log stops merely naming a
version and starts naming the exact truth set. A build that would overwrite
an existing ``v1`` directory with a DIFFERENT id refuses
(:func:`_refuse_stale_overwrite`) unless the operator passes ``--version`` to
land the new truth set in its own directory instead.

Homed in ``scripts/`` rather than in ``apps/analysis_waveform/`` for one
mechanical reason: reading a PWV6 tag means reaching into
``apps.webui.server.rb_vendor_pkg.anlz``, and a domain package importing the
delivery layer closes an ``apps.analysis_waveform <-> apps.webui`` package
cycle the arch gate counts. ``score.py`` reads this bundle's JSON and imports
no webui, so the scorer itself stays in the package where the brief puts it.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from apps.adapters.rekordbox import config
from apps.analysis_waveform import decode

# Bundle identity comes from the SCORER, which is the side that has to refuse a
# bundle it does not recognise. Restating it here is how a builder and a scorer
# drift into writing and reading different bundles under one name.
from apps.analysis_waveform.score import (
    BUNDLE_VERSION,
    CHECKSUM_FILE,
    EXPECTED_BUNDLE,
    LANE,
    compute_bundle_id,
)
from apps.shared.hashing import content_hash_bytes, sha256_audio_payload, sha256_file

BUILDER_VERSION: str = "1.0.0"
SAMPLE_SIZE: int = 50
SAMPLE_SEED: int = 20260908
# rekordbox's tri-band preview tag, the only whole-track tri-band truth that
# exists for a mapped track. 127 is its full-scale byte value (SPIKE-A1).
TRUTH_TAG: str = "PWV6"
TRUTH_SCALE: float = 127.0
BUNDLE_ID_FILE: str = "BUNDLE_ID"


def bundle_dir(data_dir: Path | None = None, *, version: str = BUNDLE_VERSION) -> Path:
    return (data_dir or config.DATA_DIR) / "bench" / LANE / version


# ----- bundle identity -------------------------------------------------------
# compute_bundle_id lives in apps.analysis_waveform.score, not here: it is the
# CONSUMER that has to re-derive it from a manifest's own contents (Codex P1
# BLOCKING, PR #1536, thread on score.py:162 - verify_bundle recomputes it to
# catch a tampered or stale manifest rather than trusting a bare string). This
# builder restates nothing; see score.py's own "Bundle identity" section.


def _refuse_stale_overwrite(out: Path, *, bundle_name: str, bundle_id: str) -> None:
    """Refuse to clobber an existing bundle directory with a DIFFERENT truth set.

    Reading and comparing happens before any file below is written, so a
    refusal leaves whatever is already at ``out`` untouched. A manifest with
    no ``bundle_id`` at all is one built before this fix; treat it the same as
    a mismatch rather than assuming it must be safe to replace. An identical
    rebuild (same id) is allowed - that is a harmless re-run, not the case
    this guards against.
    """
    existing = out / "manifest.json"
    if not existing.is_file():
        return
    old = json.loads(existing.read_text(encoding="utf-8"))
    old_id = old.get("bundle_id")
    if old_id == bundle_id:
        return
    raise SystemExit(
        f"{out} already holds bundle {old.get('bundle')!r} id {old_id!r}; this build "
        f"just sampled {bundle_name!r} id {bundle_id!r} instead - a different "
        "--sample-size, --seed, or library snapshot. Overwriting v1 in place would let "
        "two rounds claim the same fixture while measuring different tracks (Codex P1 "
        "BLOCKING, PR #1536). Pass --version <new-name> to write a new directory "
        "instead of silently regenerating this one."
    )


# ----- candidate enumeration ---------------------------------------------------


def _mapped_tracks() -> list[tuple[str, str, str | None, float | None]]:
    """``(stable_id, vendor_id, analysis_data_path, duration_s)`` for live mappings.

    Both databases are opened read-only. A missing one is a hard error, not an
    empty result: a bundle silently built from zero tracks would score a
    denominator of zero and report it as agreement.
    """
    if not config.STATE_DB.exists():
        raise SystemExit(
            f"no state db at {config.STATE_DB}; point MDT_DATA_DIR at the data "
            "directory that holds it (there is no --data-dir flag; every path here "
            "is derived from apps.adapters.rekordbox.config, which reads that env "
            "var once at import)"
        )
    if not config.MASTER_PLAIN_DB.exists():
        raise SystemExit(
            f"no decrypted rekordbox db at {config.MASTER_PLAIN_DB}; "
            "re-decrypt it before building waveform truth"
        )
    state = sqlite3.connect(f"file:{config.STATE_DB}?mode=ro", uri=True)
    try:
        vendor_by_sid = {
            str(sid): str(vid)
            for sid, vid in state.execute(
                "SELECT stable_id, vendor_id FROM track_vendor_ids "
                "WHERE vendor = 'rekordbox' AND deleted_at IS NULL"
            )
        }
    finally:
        state.close()
    master = sqlite3.connect(f"file:{config.MASTER_PLAIN_DB}?mode=ro", uri=True)
    try:
        content = {
            str(vid): (path, length)
            for vid, path, length in master.execute(
                "SELECT ID, AnalysisDataPath, Length FROM djmdContent "
                "WHERE rb_local_deleted = 0"
            )
        }
    finally:
        master.close()
    rows = []
    for sid, vid in sorted(vendor_by_sid.items()):
        found = content.get(vid)
        if found is None:
            continue
        path, length = found
        rows.append((sid, vid, path, float(length) if length else None))
    return rows


def read_truth(analysis_data_path: str | None) -> np.ndarray | None:
    """The ``(n, 3)`` PWV6 columns for one track, or None when there are none."""
    from apps.adapters.rekordbox.paths import _asset_sibling, resolve_asset_path
    from apps.webui.server.rb_vendor_pkg.anlz import _read_pwv6_tri

    if not analysis_data_path:
        return None
    mapped = resolve_asset_path(analysis_data_path)
    if mapped.resolved is None:
        return None
    sibling = _asset_sibling(mapped, mapped.resolved.with_suffix(".2EX"))
    if sibling.resolved is None or not sibling.resolved.is_file():
        return None
    return _read_pwv6_tri(sibling.resolved)


def _audio_path(stable_id: str) -> Path | None:
    from fastapi import HTTPException

    from apps.adapters.rekordbox.paths import resolve_playable_audio

    try:
        return resolve_playable_audio(stable_id, share=False).path
    except HTTPException:
        return None


def _common_audio_root(audio_paths: list[str]) -> Path:
    """The shared ancestor directory the sampled audio lives under.

    Recorded in the manifest as ``audio_root`` so ``apps.analysis_waveform.score``
    can hydrate each track's now-RELATIVE ``audio_path`` on a different host
    (Codex P1 BLOCKING, PR #1536: an absolute Mac path baked into the manifest
    meant the bundle could never score on nucbox or agentbox at all). Computed
    from each track's PARENT directory rather than the full paths themselves -
    ``os.path.commonpath`` of a single full path returns that path itself, not
    its directory, which would collapse a one-track sample's root to the file
    it names and turn every relative ``audio_path`` into an empty string.
    """
    parents = sorted({str(Path(p).parent) for p in audio_paths})
    return Path(os.path.commonpath(parents))


# ----- build -------------------------------------------------------------------


def build(
    *,
    sample_size: int = SAMPLE_SIZE,
    seed: int = SAMPLE_SEED,
    version: str = BUNDLE_VERSION,
) -> dict[str, Any]:
    """Write the bundle and return its manifest. Prints its own denominators."""
    candidates = _mapped_tracks()
    print(f"mapped rows with a live djmdContent row: {len(candidates)}")

    eligible: list[dict[str, Any]] = []
    no_truth = 0
    no_audio = 0
    for stable_id, vendor_id, adp, duration_s in candidates:
        audio = _audio_path(stable_id)
        if audio is None or not audio.is_file():
            no_audio += 1
            continue
        truth = read_truth(adp)
        if truth is None:
            no_truth += 1
            continue
        eligible.append(
            {
                "stable_id": stable_id,
                "vendor_id": vendor_id,
                "analysis_data_path": adp,
                "duration_s": duration_s,
                "audio_path": str(audio),
                "columns": int(truth.shape[0]),
            }
        )
    print(
        f"eligible (present audio AND readable {TRUTH_TAG}): {len(eligible)}; "
        f"skipped {no_audio} with no resolvable audio, {no_truth} with no {TRUTH_TAG}"
    )
    if len(eligible) < sample_size:
        raise SystemExit(
            f"only {len(eligible)} eligible tracks, need {sample_size}: refusing to "
            "build a short bundle whose denominator would then be quoted as 50"
        )

    sampled = random.Random(seed).sample(eligible, sample_size)
    sampled.sort(key=lambda row: row["stable_id"])

    # Truth JSON is built and hashed in memory FIRST, before any file below is
    # written, so compute_bundle_id() and the overwrite refusal can run against
    # an existing bundle without having already clobbered it.
    bundle_name = EXPECTED_BUNDLE if version == BUNDLE_VERSION else f"{LANE}/{version}"
    prepared: list[dict[str, Any]] = []
    for row in sampled:
        truth = read_truth(row["analysis_data_path"])
        if truth is None:  # pragma: no cover - re-read of a path just proven readable
            raise SystemExit(f"{TRUTH_TAG} vanished mid-build for {row['stable_id']}")
        truth_bytes = json.dumps(
            {
                "stable_id": row["stable_id"],
                "tag": TRUTH_TAG,
                "scale": TRUTH_SCALE,
                "columns": int(truth.shape[0]),
                "bands": {
                    name: truth[:, index].tolist() for index, name in enumerate(decode.BAND_NAMES)
                },
            }
        ).encode("utf-8")
        audio = Path(row["audio_path"])
        prepared.append(
            {
                "stable_id": row["stable_id"],
                "vendor_id": row["vendor_id"],
                "duration_s": row["duration_s"],
                "truth_bytes": truth_bytes,
                "truth_columns": int(truth.shape[0]),
                "truth_sha256": content_hash_bytes(truth_bytes),
                "audio_path": row["audio_path"],
                "audio_bytes": audio.stat().st_size,
                "audio_sha256": sha256_file(audio),
                "payload_sha256": sha256_audio_payload(audio),
            }
        )

    bundle_id = compute_bundle_id(
        seed=seed,
        sample_size=sample_size,
        tracks=[
            {
                "stable_id": r["stable_id"],
                "truth_sha256": r["truth_sha256"],
                "payload_sha256": r["payload_sha256"],
            }
            for r in prepared
        ],
    )
    out = bundle_dir(version=version)
    _refuse_stale_overwrite(out, bundle_name=bundle_name, bundle_id=bundle_id)

    audio_root = _common_audio_root([r["audio_path"] for r in prepared])

    truth_dir = out / "truth"
    truth_dir.mkdir(parents=True, exist_ok=True)
    tracks: list[dict[str, Any]] = []
    for row in prepared:
        truth_path = truth_dir / f"{row['stable_id']}.json"
        truth_path.write_bytes(row["truth_bytes"])
        relative_audio_path = Path(row["audio_path"]).relative_to(audio_root).as_posix()
        tracks.append(
            {
                "stable_id": row["stable_id"],
                "vendor_id": row["vendor_id"],
                "duration_s": row["duration_s"],
                "truth_file": f"truth/{row['stable_id']}.json",
                "truth_columns": row["truth_columns"],
                "truth_sha256": row["truth_sha256"],
                "audio_path": relative_audio_path,
                "audio_bytes": row["audio_bytes"],
                "audio_sha256": row["audio_sha256"],
                "payload_sha256": row["payload_sha256"],
            }
        )
        audio_name = Path(row["audio_path"]).name
        print(f"  {row['stable_id']} {row['truth_columns']} columns  {audio_name}")

    manifest = {
        "bundle": bundle_name,
        "bundle_id": bundle_id,
        "builder_version": BUILDER_VERSION,
        "built_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "truth_tag": TRUTH_TAG,
        "truth_scale": TRUTH_SCALE,
        "sample_seed": seed,
        "sample_size": sample_size,
        "audio_root": str(audio_root),
        "denominators": {
            "mapped_rows_with_content": len(candidates),
            "present_audio_and_readable_truth": len(eligible),
            "skipped_no_resolvable_audio": no_audio,
            "skipped_no_truth_tag": no_truth,
            "sampled": len(tracks),
        },
        "audio": (
            "NOT bundled; audio_path is relative to audio_root (portable across "
            "hosts via `apps.analysis_waveform.score --audio-root`), audio_sha256 "
            "is informational (whole file, flips on a retag), payload_sha256 (tags "
            "stripped for mp3) is what the scorer actually validates against"
        ),
        "producer": {
            "filter_graph": decode.band_filter_graph(),
            "profile": decode.PROFILE.identity(),
            "sample_rate_hz": decode.PROFILE.sample_rate_hz,
            "detail_columns_per_s": decode.PROFILE.detail_columns_per_s,
            "overview_columns": decode.PROFILE.overview_columns,
            "crossover_low_hz": decode.PROFILE.crossover_low_hz,
            "crossover_high_hz": decode.PROFILE.crossover_high_hz,
            "filter_sections": decode.PROFILE.filter_sections,
        },
        "tracks": tracks,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out / BUNDLE_ID_FILE).write_text(bundle_id + "\n", encoding="utf-8")

    sums = out / CHECKSUM_FILE
    lines = [
        f"{sha256_file(path).removeprefix('sha256:')}  {path.relative_to(out).as_posix()}"
        for path in sorted(out.rglob("*"))
        if path.is_file() and path != sums
    ]
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(lines)} files + SHA256SUMS)")
    print(f"bundle id: {bundle_id}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.build_waveform_bundle")
    sub = parser.add_subparsers(dest="command", required=True)
    build_cmd = sub.add_parser("build", help="build the PWV6 truth bundle")
    build_cmd.add_argument("--sample-size", type=int, default=SAMPLE_SIZE)
    build_cmd.add_argument("--seed", type=int, default=SAMPLE_SEED)
    build_cmd.add_argument(
        "--version",
        type=str,
        default=BUNDLE_VERSION,
        help=(
            "bundle directory name under data/bench/waveform/. A build whose truth set "
            "differs from what is already on disk under this version refuses to "
            "overwrite it; pass a NEW version to land it in its own directory instead."
        ),
    )
    args = parser.parse_args(argv)
    build(sample_size=args.sample_size, seed=args.seed, version=args.version)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
