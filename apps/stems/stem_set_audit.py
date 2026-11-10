"""Read-only stem-set quality audit against the STEM-8231 whole-set rule (STEM-10946).

MINI-PRD
--------
  ✔︎ ✅ 🎯 classify every bundle in the stems store by measured bytes
    [if] a part's bytes are FLAC [then] VIOLATION on the codec axis before
      any manifest codec claim is read
    [if] size_policy omits source_bytes or source_kbps [then] UNKNOWN, never COMPLIANT
    [if] every run starts [then] two synthetic stem-sets are checked first and
      the store is not read when they fail

This module only READS the store; it never writes or deletes inside it.

-Cursor
"""

from __future__ import annotations

import json
import math
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from apps.stems import stem_size_policy as pol
from apps.stems.artifact_types import StemArtifactError
from apps.stems.mp3_metadata import read_mp3_metadata
from apps.stems.stem_size_policy import (
    AUDIT_COMPLIANT,
    AUDIT_UNKNOWN,
    AUDIT_VIOLATION,
    MeasuredPart,
    RecordedSource,
    classify_stem_set,
)

# ----- errors and report rows ------------------------------------------------


class AuditError(RuntimeError):
    """Store missing/empty or the runtime self-test failed (CLI exit 2)."""


@dataclass(frozen=True)
class StemSetRow:
    stable_id: str
    status: str
    axis: str | None
    reason: str


@dataclass(frozen=True)
class AuditReport:
    rows: tuple[StemSetRow, ...]

    @property
    def checked(self) -> int:
        return len(self.rows)

    @property
    def compliant(self) -> int:
        return sum(1 for r in self.rows if r.status == AUDIT_COMPLIANT)

    @property
    def violation(self) -> int:
        return sum(1 for r in self.rows if r.status == AUDIT_VIOLATION)

    @property
    def unknown(self) -> int:
        return sum(1 for r in self.rows if r.status == AUDIT_UNKNOWN)

    @property
    def passed(self) -> bool:
        return self.checked > 0 and self.compliant == self.checked

    def summary_line(self) -> str:
        return f"checked={self.checked} compliant={self.compliant} violation={self.violation} unknown={self.unknown}"

    def to_json(self) -> dict[str, object]:
        return {
            "checked": self.checked,
            "compliant": self.compliant,
            "violation": self.violation,
            "unknown": self.unknown,
            "rows": [asdict(row) for row in self.rows],
        }


# ----- measure one part from bytes -------------------------------------------


def measure_part(name: str, path: Path) -> MeasuredPart:
    size = path.stat().st_size
    head = path.read_bytes()[:4]
    if head == b"fLaC":
        return MeasuredPart(name, "flac", size, None)
    if head == b"RIFF":
        return MeasuredPart(name, "wav", size, None)
    if head == b"OggS":
        return MeasuredPart(name, "opus", size, None)
    try:
        meta = read_mp3_metadata(path)
        duration_s = meta.frame_count / meta.sample_rate
        kbps = size * 8 / duration_s / 1000 if duration_s > 0 else None
        return MeasuredPart(name, "mp3", size, kbps)
    except StemArtifactError:
        return MeasuredPart(name, "unrecognised", size, None)


# ----- audit one bundle ------------------------------------------------------


def _policy_number(raw: object) -> int | float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return raw
    return None


def _read_manifest(bundle_dir: Path) -> tuple[str, dict[str, object] | None, str | None]:
    stable_id = bundle_dir.name
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file():
        return stable_id, None, "no manifest.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return stable_id, None, f"manifest.json unreadable: {exc}"
    if not isinstance(raw, dict):
        return stable_id, None, "manifest.json unreadable: not an object"
    return stable_id, raw, None


def _measured_parts(bundle_dir: Path, files: dict[str, object]) -> tuple[list[MeasuredPart], str | None]:
    parts: list[MeasuredPart] = []
    for part_name, filename in files.items():
        if not isinstance(part_name, str) or not isinstance(filename, str):
            return [], "manifest lists no parts"
        part_path = bundle_dir / filename
        if not part_path.is_file():
            return [], f"{part_name}: {filename} missing"
        parts.append(measure_part(part_name, part_path))
    return parts, None


def _recorded_source(raw: dict[str, object]) -> RecordedSource:
    source_raw = raw.get("source")
    src_path: str | None = None
    if isinstance(source_raw, dict):
        p = source_raw.get("path")
        if isinstance(p, str) and p and p != "unknown":
            src_path = p
    ext = Path(src_path).suffix.lower() if src_path else None
    policy = raw.get("size_policy")
    source_bytes: int | None = None
    source_kbps: float | None = None
    if isinstance(policy, dict):
        nbytes = _policy_number(policy.get("source_bytes"))
        if nbytes is not None:
            source_bytes = int(nbytes)
        nkbps = _policy_number(policy.get("source_kbps"))
        if nkbps is not None:
            source_kbps = float(nkbps)
    return RecordedSource(ext=ext, source_bytes=source_bytes, source_kbps=source_kbps)


def audit_stem_set(bundle_dir: Path) -> StemSetRow:
    stable_id, raw, err = _read_manifest(bundle_dir)
    if err is not None or raw is None:
        return StemSetRow(stable_id, AUDIT_UNKNOWN, None, err or "no manifest.json")
    files = raw.get("files")
    if not isinstance(files, dict) or not files:
        return StemSetRow(stable_id, AUDIT_UNKNOWN, None, "manifest lists no parts")
    parts, part_err = _measured_parts(bundle_dir, files)
    if part_err is not None:
        return StemSetRow(stable_id, AUDIT_UNKNOWN, None, part_err)
    layout = raw.get("layout") if isinstance(raw.get("layout"), str) else "unknown"
    verdict = classify_stem_set(
        layout=layout,
        parts=parts,
        source=_recorded_source(raw),
    )
    return StemSetRow(stable_id, verdict.status, verdict.axis, verdict.reason)


def audit_store(root: Path) -> AuditReport:
    if not root.is_dir():
        raise AuditError(f"stems store {root} does not exist")
    rows: list[StemSetRow] = []
    for child in sorted(root.iterdir()):
        if child.name.startswith(".") or not child.is_dir() or child.is_symlink():
            continue
        rows.append(audit_stem_set(child))
    if not rows:
        raise AuditError(f"stems store {root} holds no stem-sets; an empty store is not a pass")
    return AuditReport(tuple(rows))


# ----- synthetic fixtures (self-test + tests share these) --------------------

_MPEG1_L3_BITRATE_IDX: dict[int, int] = {
    32: 1,
    40: 2,
    48: 3,
    56: 4,
    64: 5,
    80: 6,
    96: 7,
    112: 8,
    128: 9,
    160: 10,
    192: 11,
    224: 12,
    256: 13,
    320: 14,
}


def synthetic_cbr_mp3_bytes(kbps: int, seconds: float, sample_rate: int = 44_100) -> bytes:
    """MPEG-1 Layer III mono CBR frames with zero audio payload (header geometry only)."""
    if kbps not in _MPEG1_L3_BITRATE_IDX:
        raise ValueError(f"kbps {kbps} has no MPEG-1 L3 index in this helper")
    bitrate_idx = _MPEG1_L3_BITRATE_IDX[kbps]
    sr_idx = {44_100: 0, 48_000: 1, 32_000: 2}[sample_rate]
    pad = 0
    frame_len = 144_000 * kbps // sample_rate + pad
    frame_count = math.ceil(seconds * sample_rate / 1152)
    out = bytearray()
    byte2 = (bitrate_idx << 4) | (sr_idx << 2) | (pad << 1)
    header = bytes((0xFF, 0xFB, byte2, 0xC0))
    payload_len = max(0, frame_len - 4)
    for _ in range(frame_count):
        out.extend(header)
        out.extend(bytes(payload_len))
    return bytes(out)


def write_synthetic_stem_set(
    root: Path,
    stable_id: str,
    *,
    kind: Literal["flac_from_mp3", "compliant", "unrecorded"],
) -> Path:
    bundle = root / stable_id
    bundle.mkdir(parents=True, exist_ok=True)
    parts = ("bass", "drums", "other", "vocals")
    files: dict[str, str] = {}
    if kind == "flac_from_mp3":
        for part in parts:
            name = f"{part}.flac"
            (bundle / name).write_bytes(b"fLaC" + bytes(4096))
            files[part] = name
        manifest: dict[str, object] = {
            "layout": pol.WHOLE_SET_LAYOUT,
            "files": files,
            "source": {"path": str(root / "src" / "track.mp3")},
        }
    elif kind in ("compliant", "unrecorded"):
        for part in parts:
            target = 96 if part in pol.MP3_RHYTHM_PARTS else 80
            ext = "mp3"
            name = f"{part}.{ext}"
            (bundle / name).write_bytes(synthetic_cbr_mp3_bytes(target, 2.0))
            files[part] = name
        manifest = {
            "layout": pol.WHOLE_SET_LAYOUT,
            "files": files,
            "source": {"path": str(root / "src" / "track.mp3")},
        }
        if kind == "compliant":
            source_bytes = 320 * 1000 * 2 // 8 + 4096
            manifest["size_policy"] = {"source_bytes": source_bytes, "source_kbps": 320.0}
    else:
        raise ValueError(f"unknown synthetic kind {kind!r}")
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return bundle


SELF_TEST_FLAC_ID = "__self_test_flac_from_mp3__"
SELF_TEST_COMPLIANT_ID = "__self_test_compliant__"


def run_self_test() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_synthetic_stem_set(root, SELF_TEST_FLAC_ID, kind="flac_from_mp3")
        write_synthetic_stem_set(root, SELF_TEST_COMPLIANT_ID, kind="compliant")
        report = audit_store(root)
        by_id = {r.stable_id: r for r in report.rows}
        expected = {
            SELF_TEST_FLAC_ID: (AUDIT_VIOLATION, pol.AXIS_CODEC),
            SELF_TEST_COMPLIANT_ID: (AUDIT_COMPLIANT, None),
        }
        for sid, (status, axis) in expected.items():
            row = by_id.get(sid)
            if row is None or row.status != status or row.axis != axis:
                raise AuditError(
                    f"self-test failed: expected {sid}={status} axis={axis}, "
                    f"got {None if row is None else row.status} axis={None if row is None else row.axis}"
                )


def run_audit(root: Path) -> AuditReport:
    run_self_test()
    return audit_store(root)
