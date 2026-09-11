"""Read the GTZAN corpus and the runner shards, and prove they are the same ones.

This half of the GTZAN bench does no scoring. It answers a prior question: WHAT
was measured, and by WHICH analyzer. Every guard in here exists because an
artifact that cannot answer those two questions is presented as canonical human
ground truth while being unable to show it (three Codex P1 BLOCKINGs on PR
#1660, all of the same shape).

It is separate from `score_gtzan.py` for a reason that predates the file split:
`mir_eval` is a bench-only dependency absent from the repo venv, so anything
sharing a module with the scoring functions was unimportable in the test lane.
That is how the merge and provenance guards below came to carry two
benchmark-integrity checks with no test at all. Nothing here imports `mir_eval`
or `numpy`, so `tests/beatbench/test_shard_provenance.py` exercises the real
code rather than skipping it, and a skip is never mistaken for a pass.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def fixtures_digest(requested: dict[str, Any]) -> str:
    """One digest over every fixture byte the run ASKED FOR, scored or not.

    Path matching alone accepted ANY extraction sitting at the same relative
    filenames, so a different GTZAN repack or an edited annotation set produced
    an artifact presented as canonical human ground truth with nothing in it to
    show otherwise (Codex P1 BLOCKING, PR #1660). This binds the artifact to
    the exact bytes on both sides: `annotation_sha256` is the reference, and
    `decode_fingerprint` is the runner's hash of the DECODED audio samples, so
    a re-encode or a different clip changes it even when the filename does not.

    REQUESTED, NOT SCORED, and that distinction is the whole gate. This used to
    digest only the clips that produced a row, which excludes every runner
    error -- and GTZAN's corrupt `jazz_00054` is the only one of those, so
    deleting that pair from the manifest left the same 998 scored clips, the
    same digest, AND removed the only expected `runner_error`, which is the
    other thing `gates.refusals` looks at. The canonical 999-pair manifest
    could therefore be truncated to 998 and the run would exit 0 reporting a
    matching fixture digest and a clean error list (Codex P1 BLOCKING, PR
    #1660, discussion_r3977265430). Now every requested key contributes a line,
    so removing a pair removes a line and the digest moves, whatever the reason
    that pair could not be scored.

    An unscorable pair contributes what is KNOWN about it rather than being
    skipped: the annotation bytes are readable whether or not the audio
    decoded, and a clip with no decode contributes `None` in that position,
    which is a distinct line from the same clip decoding successfully.

    Pass the resulting digest back as `--expect-fixtures-digest` to make a
    later round assert it is scoring the same fixtures, rather than assume it.
    """
    parts = [
        f"{key}|{clip.get('annotation_sha256')}|{clip.get('decode_fingerprint')}"
        f"|{clip.get('audio_sha256')}"
        for key, clip in sorted(requested.items())
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    """sha256 of a file's bytes, read in blocks so a 1.2 GB corpus stays flat.

    Read for EVERY requested pair. Measured Thu 10 Sep 2026: 999 GTZAN files,
    1.32 GB, 6.6 s -- which is why the "too expensive" argument for hashing
    only the undecodable ones did not survive being checked.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_inputs_for(
    pairs: dict[str, Any], results: dict[str, dict[str, Any]], audio_root: str
) -> dict[str, dict[str, Any]]:
    """What `fixtures_digest` hashes: one entry per REQUESTED pair.

    Deliberately separate from scoring, and deliberately free of `mir_eval`.
    The fixture gate is a property of the CORPUS -- which annotation bytes and
    which decoded audio -- not of how well the analyzer did on it, and putting
    it inside the scorer is what let it see only the clips that scored. It also
    means the gate can be tested in this repository's own environment, while
    `scripts/beatbench/score_gtzan.py` is a PEP 723 script whose scorer cannot
    be imported here at all.

    A pair with no result, or a result the runner marked `error`, contributes
    an explicit `None` decode fingerprint rather than dropping out. GTZAN's
    corrupt `jazz_00054` is the only such pair in the canonical manifest, so
    when it dropped out, deleting it from the manifest changed neither the
    digest nor the expected-error list and a truncated 999-pair manifest scored
    clean (Codex P1 BLOCKING, PR #1660, discussion_r3977265430).

    AND EVERY PAIR ALSO CARRIES `audio_sha256`, THE RAW BYTES. `None` is not an
    identity: a clip that fails to decode contributed the same `None` whatever
    was at its path, so swapping the corrupt fixture for any other undecodable
    file left the digest unchanged (Codex P1 BLOCKING, PR #1660,
    discussion_r3977480630).

    That was first fixed for the undecodable pairs ONLY, on the argument that
    `decode_fingerprint` already identifies a decodable one and that hashing
    1.2 GB per scoring run was too expensive to re-answer it. The second half
    of that argument was wrong and a measurement settled it: hashing all 999
    GTZAN files takes **6.6 s**, against 535 s for the analysis they are scored
    from. And the first half concedes too much -- this gate documents itself as
    binding the artifact to the exact BYTES, the annotation side is hashed raw,
    and a corpus re-containered or re-tagged while preserving its decoded
    samples is no longer the fixture set the recorded digest was taken over
    (Codex P1 BLOCKING, PR #1660, discussion_r3977660369).

    Both fields are kept, because they answer different questions and the
    benchmark asks both: `audio_sha256` says these are the same FILES, and
    `decode_fingerprint` says the analyzer read the same SAMPLES out of them.
    A decoder change that alters samples without touching a byte on disk moves
    only the second.
    """
    inputs: dict[str, dict[str, Any]] = {}
    for key, (wav, beats_path) in sorted(pairs.items()):
        full = str(Path(audio_root) / wav) if audio_root else wav
        annotation = Path(audio_root) / beats_path if audio_root else Path(beats_path)
        audio = Path(audio_root) / wav if audio_root else Path(wav)
        row = results.get(full) or results.get(wav)
        fingerprint = None if row is None else row.get("decode_fingerprint")
        inputs[key] = {
            "annotation_sha256": hashlib.sha256(annotation.read_bytes()).hexdigest(),
            "decode_fingerprint": fingerprint,
            "audio_sha256": _file_sha256(audio),
        }
    return inputs


def load_annotation(path: Path) -> tuple[list[float], list[float]]:
    """`(beat times, downbeat times)` from a GTZAN-Rhythm `.beats` file.

    Format is `<seconds>\\t<position in bar>` per line, position 1 being the
    downbeat.
    """
    beats: list[float] = []
    downbeats: list[float] = []
    for raw_line in path.read_text().splitlines():
        parts = raw_line.split()
        if not parts:
            continue
        t = float(parts[0])
        beats.append(t)
        if len(parts) > 1 and parts[1] == "1":
            downbeats.append(t)
    return beats, downbeats


# The header fields that define WHICH analyzer produced a shard. Two shards
# that disagree on any of them did not measure the same thing.
#
# `gpu` is here because `device` cannot carry the accelerator's identity: an
# L4 shard and an H100 shard both say "cuda", so without this key they merge
# as one benchmark. It is the accelerator MODEL, null on a CPU producer.
PROVENANCE_KEYS = (
    "producer", "producer_version", "beat_this_version", "torch_version",
    # The decode and inference dependencies, and the analyzer's own code
    # identity. Both producers RECORDED these and this identity excluded them,
    # so shards decoded by a different soundfile, run against a different
    # numpy, or produced by a different revision of the runner still merged as
    # one benchmark whenever the rest of the header was unchanged, and the
    # merged artifact then dropped the differing values entirely (Codex P1
    # BLOCKING, PR #1660).
    # `torchaudio_version` is the DECODER's, and it was the last field in this
    # tuple to be added because it was the last to be noticed: `soundfile` was
    # recorded from the start and is only the FALLBACK path inside
    # `beat_this.inference.load_audio`, which tries `torchaudio.load` first
    # (Codex P1 BLOCKING, PR #1660, discussion_r3977265421). Two shards decoded
    # by different torchaudio releases merged as one benchmark while the field
    # that would have shown it went unrecorded.
    "numpy_version", "torchaudio_version", "soundfile_version", "runner_sha256",
    "checkpoint", "model_sha256", "device", "gpu", "threshold", "fps",
)

#: The one provenance key whose null is MEANINGFUL rather than missing: a CPU
#: producer has no accelerator to name. Every other key is required to carry a
#: value, because the guard below is otherwise satisfied by shards that all
#: failed to determine the same thing.
NULLABLE_PROVENANCE_KEYS = frozenset({"gpu"})


def load_results(paths: list[Path]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Merge per-shard runner JSON, refusing to merge shards that disagree.

    The reproduction instructions recommend sharding, so the multi-shard path
    is the NORMAL one, and it silently discarded every shard's checkpoint
    digest, threshold and fps. One stale shard left over from an earlier
    threshold then produced a hybrid benchmark carrying no provenance at all,
    and nothing in the artifact could reveal it afterwards (Codex P1 BLOCKING,
    PR #1660). Now a disagreement is fatal at merge time and the agreed
    provenance is carried into the scored artifact.

    A duplicate audio key across shards is also fatal: shards partition a
    corpus, so an overlap means the same clip was analyzed twice, possibly
    differently, and `dict.update` would have silently kept whichever shard
    was listed last.

    A MISSING provenance field is fatal too, and that is the half the first
    version of this guard got wrong. It read each key with `payload.get`, so a
    producer that emitted none of them gave every shard `None`, the shards
    compared equal, and the check passed while proving nothing. That was not
    hypothetical: `modal_beat_farm.py` emitted no `producer_version`,
    `beat_this_version` or `torch_version` at all, which are exactly the
    fields that would reveal two shards built against different dependencies
    (Codex P1 BLOCKING, PR #1660). A check that cannot fail for the reason it
    exists is not a check, so absence is now refused by name.
    """
    merged: dict[str, dict[str, Any]] = {}
    provenance: dict[str, Any] = {}
    source: dict[str, str] = {}
    for path in paths:
        payload = json.loads(path.read_text())
        absent = [key for key in PROVENANCE_KEYS if key not in payload]
        if absent:
            raise SystemExit(
                f"error: {path} is missing required analyzer provenance: "
                f"{', '.join(absent)}. A shard that does not say what produced "
                "it cannot be shown to have measured the same thing as its "
                "siblings, and defaulting the field to null would make every "
                "such shard compare equal to every other."
            )
        empty = [
            key for key in PROVENANCE_KEYS
            if key not in NULLABLE_PROVENANCE_KEYS
            and (payload[key] is None or payload[key] == "")
        ]
        if empty:
            raise SystemExit(
                f"error: {path} carries required analyzer provenance with no "
                f"value: {', '.join(empty)}. Presence was checked and value was "
                "not, which is the same defect one level down: "
                "`resolve_checkpoint_sha256` returns None when it cannot find "
                "the weights that were loaded, and a null agrees with every "
                "other null, so shards analyzed with UNKNOWN weights compared "
                "equal and merged (Codex P1 BLOCKING, PR #1660). The only key "
                f"whose null is meaningful is {sorted(NULLABLE_PROVENANCE_KEYS)}, "
                "where it says there was no accelerator."
            )
        shard = {key: payload[key] for key in PROVENANCE_KEYS}
        if not provenance:
            provenance = shard
            provenance_source = path
        elif shard != provenance:
            differing = {
                key: {str(provenance_source): provenance[key], str(path): shard[key]}
                for key in PROVENANCE_KEYS if shard[key] != provenance[key]
            }
            raise SystemExit(
                "error: shards disagree on analyzer provenance, so merging them "
                "would produce a hybrid benchmark:\n"
                + json.dumps(differing, indent=2)
            )
        results = payload.get("results", {})
        rows = results.items() if isinstance(results, dict) else (
            (r["audio"], r) for r in results)
        for key, row in rows:
            if key in merged:
                raise SystemExit(
                    f"error: {key!r} appears in both {source[key]} and {path}. "
                    "Shards must partition the corpus; an overlap means one clip "
                    "was analyzed twice and only one result would survive."
                )
            merged[key] = row
            source[key] = str(path)
    return merged, provenance
