"""Structural diff between two open-dj library documents."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROVENANCE_FIELDS: frozenset[str] = frozenset(
    {"bpm", "key", "energy", "rating"}
)


@dataclass
class FieldChange:
    field_name: str
    before: Any
    after: Any


@dataclass
class TrackDiff:
    track_id: str
    changes: list[FieldChange] = field(default_factory=list)


@dataclass
class DiffReport:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    mutated: list[TrackDiff] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.mutated)


def diff_documents(a: dict[str, Any], b: dict[str, Any]) -> DiffReport:
    """Return a :class:`DiffReport` comparing tracks of ``a`` vs ``b``."""
    tracks_a = {t["track_id"]: t for t in a.get("tracks", [])}
    tracks_b = {t["track_id"]: t for t in b.get("tracks", [])}

    report = DiffReport()
    report.removed = sorted(tracks_a.keys() - tracks_b.keys())
    report.added = sorted(tracks_b.keys() - tracks_a.keys())

    for track_id in sorted(tracks_a.keys() & tracks_b.keys()):
        changes = _diff_track(tracks_a[track_id], tracks_b[track_id])
        if changes:
            report.mutated.append(TrackDiff(track_id=track_id, changes=changes))
    return report


def _diff_track(a: dict[str, Any], b: dict[str, Any]) -> list[FieldChange]:
    changes: list[FieldChange] = []
    all_keys = set(a.keys()) | set(b.keys())
    for key in sorted(all_keys):
        if key == "track_id":
            continue
        av = a.get(key)
        bv = b.get(key)
        if key in PROVENANCE_FIELDS:
            for sub in _diff_provenance(key, av, bv):
                changes.append(sub)
            continue
        if av != bv:
            changes.append(FieldChange(field_name=key, before=av, after=bv))
    return changes


def _diff_provenance(
    field_name: str, a: Any, b: Any
) -> list[FieldChange]:
    out: list[FieldChange] = []
    if a is None and b is None:
        return out
    if a is None or b is None:
        return [FieldChange(field_name=field_name, before=a, after=b)]
    for sub in ("value", "source", "confidence", "modified_at"):
        av = a.get(sub)
        bv = b.get(sub)
        if av != bv:
            out.append(FieldChange(
                field_name=f"{field_name}.{sub}", before=av, after=bv
            ))
    return out


def format_report(report: DiffReport) -> str:
    if report.is_empty:
        return "no differences"
    lines: list[str] = []
    if report.added:
        lines.append("added tracks:")
        lines.extend(f"  + {tid}" for tid in report.added)
    if report.removed:
        lines.append("removed tracks:")
        lines.extend(f"  - {tid}" for tid in report.removed)
    if report.mutated:
        lines.append("mutated tracks:")
        for td in report.mutated:
            lines.append(f"  ~ {td.track_id}")
            for ch in td.changes:
                lines.append(
                    f"      {ch.field_name}: {ch.before!r} -> {ch.after!r}"
                )
    return "\n".join(lines)
