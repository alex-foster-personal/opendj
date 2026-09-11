"""PERFBATCH-01 library-processing wait-time KPIs.

Derives per-track wall, queue throughput, and library ETA for the stems and
lyrics pipelines from artifacts those pipelines already write. Observation
only: this module does not stamp either pipeline and does not change
throughput or latency.

    python -m scripts.perf.library_wait --data-dir DIR [--json]
"""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scripts.bench.kpi_derive import RUN_GAP_S
from scripts.perf import library_wait_lyrics as lyrics_side
from scripts.perf import library_wait_stems as stems_side

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_LEDGER = REPO_ROOT / "docs" / "perf" / "kpi-ledger.json"
SOURCE = "scripts.perf.library_wait"
ROUND = "issue-1704"

KPI_NAMES: tuple[str, ...] = (
    "stems_per_track_wall_s",
    "stems_queue_throughput_per_h",
    "stems_library_eta_s",
    "lyrics_per_track_wall_s",
    "lyrics_queue_throughput_per_h",
    "lyrics_library_eta_s",
)
UNITS: dict[str, str] = {
    "stems_per_track_wall_s": "s",
    "stems_queue_throughput_per_h": "tracks/h",
    "stems_library_eta_s": "s",
    "lyrics_per_track_wall_s": "s",
    "lyrics_queue_throughput_per_h": "tracks/h",
    "lyrics_library_eta_s": "s",
}


class InstrumentError(ValueError):
    """The instrument itself is broken (dishonest denominator, mixed capture)."""


@dataclass(frozen=True)
class Availability:
    present: int
    awaiting_volume: int
    absent: int
    streaming: int
    present_ids: frozenset[str]
    checked_at: str


@dataclass(frozen=True)
class Figure:
    kpi: str
    unit: str
    value: float | None
    note: str
    measured: bool
    capture_id: str
    status: str | None = None


@dataclass(frozen=True)
class WaitSnapshot:
    capture_id: str
    machine: str
    checked_at: str
    figures: tuple[Figure, ...]

    def by_name(self, kpi: str) -> Figure:
        for figure in self.figures:
            if figure.kpi == kpi:
                return figure
        raise KeyError(kpi)


@dataclass(frozen=True)
class PublicationWindow:
    wall_s: float
    n: int


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def remaining_present_without(present_ids: frozenset[str], have: frozenset[str]) -> frozenset[str]:
    return present_ids - have


def check_remaining_is_present_only(remaining: frozenset[str], present_ids: frozenset[str]) -> None:
    extra = remaining - present_ids
    if extra:
        raise InstrumentError(f"remaining includes non-present ids: {sorted(extra)[:5]}")


def validate_figure(figure: Figure) -> None:
    if figure.value is None:
        return
    if "denominator=present" not in figure.note:
        raise InstrumentError(f"{figure.kpi}: numeric figure missing denominator=present")
    if "denominator=tracks" in figure.note:
        raise InstrumentError(f"{figure.kpi}: quoted tracks row count as denominator")


def validate_snapshot(snapshot: WaitSnapshot) -> None:
    names = [figure.kpi for figure in snapshot.figures]
    if names != list(KPI_NAMES):
        raise InstrumentError(f"expected {list(KPI_NAMES)}, got {names}")
    ids = {figure.capture_id for figure in snapshot.figures}
    if ids != {snapshot.capture_id}:
        raise InstrumentError(f"mixed capture_id values: {sorted(ids)}")
    for figure in snapshot.figures:
        validate_figure(figure)


def _denom_note(availability: Availability | None, extra: str) -> str:
    if availability is None:
        return (
            f"denominator=present withheld (no state.db; not the tracks row count); {extra}"
        )
    checked = availability.checked_at
    if availability.awaiting_volume:
        base = (
            f"denominator=present {availability.present} + awaiting_volume="
            f"{availability.awaiting_volume} (checked {checked})"
        )
    else:
        base = f"denominator=present {availability.present} (checked {checked})"
    return f"{base}; {extra}"


def _withheld(kpi: str, capture_id: str, note: str) -> Figure:
    return Figure(
        kpi=kpi,
        unit=UNITS[kpi],
        value=None,
        note=note,
        measured=False,
        capture_id=capture_id,
        status="withheld",
    )


def _numeric(kpi: str, value: float, capture_id: str, note: str) -> Figure:
    figure = Figure(
        kpi=kpi,
        unit=UNITS[kpi],
        value=value,
        note=note,
        measured=True,
        capture_id=capture_id,
        status=None,
    )
    validate_figure(figure)
    return figure


def _latest_cluster(mtimes: list[float]) -> list[float]:
    ordered = sorted(mtimes)
    cut = 0
    for i in range(1, len(ordered)):
        if ordered[i] - ordered[i - 1] > RUN_GAP_S:
            cut = i
    return ordered[cut:]


def publication_window(mtimes: list[float]) -> PublicationWindow | None:
    cluster = _latest_cluster(mtimes)
    if len(cluster) < 2:
        return None
    wall_s = cluster[-1] - cluster[0]
    if wall_s <= 0:
        return None
    return PublicationWindow(wall_s=wall_s, n=len(cluster))


def _mtime_jsons(directory: Path, *, require_container_s: bool) -> list[float]:
    if not directory.is_dir():
        return []
    found: list[float] = []
    for path in directory.glob("*.json"):
        if require_container_s:
            try:
                entry = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            timings = entry.get("worker", {}).get("timings", {})
            if "container_s" not in timings:
                continue
        found.append(path.stat().st_mtime)
    return found


def _rate_figures(
    *,
    wall_kpi: str,
    rate_kpi: str,
    window: PublicationWindow | None,
    capture_id: str,
    availability: Availability | None,
    extra: str,
) -> tuple[Figure, Figure]:
    if window is None:
        note = _denom_note(availability, f"empty publication window; {extra}")
        return _withheld(wall_kpi, capture_id, note), _withheld(rate_kpi, capture_id, note)
    per_track = window.wall_s / window.n
    per_h = window.n / window.wall_s * 3600.0
    note = _denom_note(
        availability,
        f"{extra}; publication window n={window.n} wall_s={window.wall_s:.1f}",
    )
    return (
        _numeric(wall_kpi, round(per_track, 2), capture_id, note),
        _numeric(rate_kpi, round(per_h, 1), capture_id, note),
    )


def stems_remaining_present(data_dir: Path, availability: Availability) -> frozenset[str]:
    remaining = stems_side.remaining_present(availability.present_ids, data_dir)
    check_remaining_is_present_only(remaining, availability.present_ids)
    return remaining


def lyrics_remaining_present(data_dir: Path, availability: Availability) -> frozenset[str]:
    remaining = lyrics_side.remaining_present(availability.present_ids, data_dir)
    check_remaining_is_present_only(remaining, availability.present_ids)
    return remaining


def probe_availability(state_db: Path, checked_at: str) -> Availability | None:
    if not state_db.is_file():
        return None
    from apps.mik.availability import probe

    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        rows = probe(conn)
    finally:
        conn.close()
    present_ids = frozenset(row.stable_id for row in rows if row.state == "present")
    counts = {"present": 0, "awaiting_volume": 0, "absent": 0, "streaming": 0}
    for row in rows:
        counts[row.state] = counts.get(row.state, 0) + 1
    return Availability(
        present=counts["present"],
        awaiting_volume=counts["awaiting_volume"],
        absent=counts["absent"],
        streaming=counts["streaming"],
        present_ids=present_ids,
        checked_at=checked_at,
    )


def _eta_figure(
    kpi: str,
    *,
    remaining: int,
    tracks_per_s: float | None,
    capture_id: str,
    availability: Availability,
    extra: str,
) -> Figure:
    note = _denom_note(availability, extra)
    if tracks_per_s is None or tracks_per_s <= 0:
        return _withheld(kpi, capture_id, note + "; rate unmeasured")
    return _numeric(kpi, round(remaining / tracks_per_s, 1), capture_id, note)


def capture_snapshot(
    data_dir: Path,
    *,
    capture_id: str,
    machine: str,
    now: str,
    availability: Availability | None = None,
) -> WaitSnapshot:
    data_dir = Path(data_dir)
    resolved = availability
    if resolved is None:
        resolved = probe_availability(data_dir / "state" / "state.db", now)
    stems_window = publication_window(
        _mtime_jsons(data_dir / "state" / "vocal-cache", require_container_s=True)
    )
    lyrics_window = publication_window(
        _mtime_jsons(data_dir / "state" / "lyrics-cache", require_container_s=False)
    )
    stems_wall, stems_rate = _rate_figures(
        wall_kpi="stems_per_track_wall_s",
        rate_kpi="stems_queue_throughput_per_h",
        window=stems_window,
        capture_id=capture_id,
        availability=resolved,
        extra="stems publication window from vocal-cache container_s entries",
    )
    lyrics_wall, lyrics_rate = _rate_figures(
        wall_kpi="lyrics_per_track_wall_s",
        rate_kpi="lyrics_queue_throughput_per_h",
        window=lyrics_window,
        capture_id=capture_id,
        availability=resolved,
        extra="lyrics publication window from lyrics-cache mtimes",
    )
    if resolved is None:
        stems_eta = _withheld(
            "stems_library_eta_s",
            capture_id,
            _denom_note(None, "library-scale ETA withheld; remaining needs present ids"),
        )
        lyrics_eta = _withheld(
            "lyrics_library_eta_s",
            capture_id,
            _denom_note(None, "library-scale ETA withheld; remaining needs present ids"),
        )
    else:
        stems_left = stems_remaining_present(data_dir, resolved)
        stems_per_s = None if stems_window is None else stems_window.n / stems_window.wall_s
        stems_eta = _eta_figure(
            "stems_library_eta_s",
            remaining=len(stems_left),
            tracks_per_s=stems_per_s,
            capture_id=capture_id,
            availability=resolved,
            extra=f"remaining_present_without_bundle={len(stems_left)}",
        )
        lyrics_left = lyrics_remaining_present(data_dir, resolved)
        fetch_per_s = None if lyrics_window is None else lyrics_window.n / lyrics_window.wall_s
        eta_s, extra = lyrics_side.eta_seconds(
            len(lyrics_left),
            fetch_per_s,
            lyrics_side.read_index_meta(data_dir / "state" / "lyrics-index.db"),
        )
        if eta_s is None:
            lyrics_eta = _withheld(
                "lyrics_library_eta_s", capture_id, _denom_note(resolved, extra)
            )
        else:
            lyrics_eta = _numeric(
                "lyrics_library_eta_s",
                round(eta_s, 1),
                capture_id,
                _denom_note(resolved, extra),
            )
    figures = (stems_wall, stems_rate, stems_eta, lyrics_wall, lyrics_rate, lyrics_eta)
    snapshot = WaitSnapshot(capture_id, machine, now, figures)
    validate_snapshot(snapshot)
    return snapshot


def snapshot_to_json(snapshot: WaitSnapshot) -> dict:
    kpis = []
    for figure in snapshot.figures:
        row = {
            "kpi": figure.kpi,
            "value": figure.value,
            "unit": figure.unit,
            "measured": figure.measured,
            "note": figure.note,
            "capture_id": figure.capture_id,
        }
        if figure.status is not None:
            row["status"] = figure.status
        kpis.append(row)
    return {
        "capture_id": snapshot.capture_id,
        "machine": snapshot.machine,
        "checked_at": snapshot.checked_at,
        "kpis": kpis,
    }


def append_ledger_rows(
    ledger_path: Path,
    figures: list[Figure] | tuple[Figure, ...],
    *,
    date: str,
    machine: str,
) -> None:
    for figure in figures:
        validate_figure(figure)
    payload = json.loads(ledger_path.read_text())
    entries = payload.setdefault("entries", [])
    for figure in figures:
        row = {
            "date": date,
            "round": ROUND,
            "kpi": figure.kpi,
            "value": figure.value,
            "unit": figure.unit,
            "machine": machine,
            "source": SOURCE,
            "note": figure.note,
            "capture_id": figure.capture_id,
        }
        if figure.status is not None:
            row["status"] = figure.status
        if figure.measured is False:
            row["measured"] = False
        entries.append(row)
    ledger_path.write_text(json.dumps(payload, indent=1) + "\n")


def _print_table(snapshot: WaitSnapshot) -> None:
    print(
        f"capture_id={snapshot.capture_id} machine={snapshot.machine} "
        f"checked_at={snapshot.checked_at}"
    )
    width = max(len(name) for name in KPI_NAMES)
    for figure in snapshot.figures:
        shown = "withheld" if figure.value is None else figure.value
        print(f"{figure.kpi.ljust(width)}  {shown} {figure.unit}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="library_wait")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--require-measured", action="store_true")
    parser.add_argument("--capture-id", default="")
    parser.add_argument("--machine", default="")
    parser.add_argument("--append", nargs="?", const=str(DEFAULT_LEDGER), default=None)
    args = parser.parse_args(argv)

    now = utc_now()
    machine = args.machine or socket.gethostname()
    capture_id = args.capture_id or f"issue-1704-{machine}-{now.replace(':', '').replace('+', 'p')}"
    try:
        snapshot = capture_snapshot(
            Path(args.data_dir), capture_id=capture_id, machine=machine, now=now
        )
    except InstrumentError as exc:
        print(f"[library-wait] broken instrument: {exc}", file=sys.stderr)
        return 1

    if args.require_measured:
        missing = [figure.kpi for figure in snapshot.figures if not figure.measured]
        if missing:
            print(
                f"[library-wait] --require-measured and unmeasured: {', '.join(missing)}",
                file=sys.stderr,
            )
            return 1

    if args.json:
        print(json.dumps(snapshot_to_json(snapshot), indent=2))
    else:
        _print_table(snapshot)

    if args.append is not None:
        try:
            append_ledger_rows(
                Path(args.append), snapshot.figures, date=now[:10], machine=machine
            )
        except InstrumentError as exc:
            print(f"[library-wait] ledger refuse: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
