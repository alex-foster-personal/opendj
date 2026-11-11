"""Ledger row builders and atomic append for the S5/S12 KPI capture.

Append-only: never edit or delete an existing entry. A failed probe writes
``value: null`` / ``status: error`` / ``measured: false``, never a number.
"""

from __future__ import annotations

import re
import socket
import sqlite3
import subprocess
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import Request

from apps.mik.availability import probe

_S12_TRACKS_DENOM_RE = re.compile(r"denominator=(\d+)\s+tracks")
_S12_PRESENT_DENOM_RE = re.compile(r"denominator=present\s+(\d+)")

SOURCE = "scripts.perf.capture_kpis"
ROUND = "perf-capture"
CAPTURE_TAG = "capture=perf-capture"
S5_METHOD = "GET /api/v1/tracks/{id}/anlz?points=38400 + GET /audio (full body), n=5 median, max of 3 tracks"
S12_METHOD = "apps.sync_hub.sync_timing.PhaseTimer via run_sync HttpTransport"
S5_REQUIRED = ("packaged_deck_load_total_ms",)
S12_REQUIRED = ("cloudsync_first_sync_s", "cloudsync_noop_sync_s")
S5_UNIT = "ms"
S12_UNIT = "s"
LIBRARY_SCALE_MIN_TRACKS = 1000
HEALTH_TIMEOUT_S = 60
ANLZ_TIMEOUT_S = 60
AUDIO_TIMEOUT_S = 300
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = REPO_ROOT / "docs" / "perf" / "kpi-ledger.json"


@dataclass(frozen=True)
class CaptureMeta:
    capture_id: str
    date: str
    machine: str
    sha: str


@dataclass(frozen=True)
class S12Denominator:
    track_count: int
    present: int
    unreachable: int


def probe_s12_denominator(state_db: Path) -> S12Denominator | None:
    if not state_db.is_file():
        return None
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT count(*) FROM tracks WHERE deleted_at IS NULL").fetchone()
        track_count = 0 if row is None else int(row[0])
        rows = probe(conn)
    finally:
        conn.close()
    present = sum(1 for item in rows if item.state == "present")
    unreachable = sum(1 for item in rows if item.state not in ("present", "streaming"))
    return S12Denominator(track_count=track_count, present=present, unreachable=unreachable)


def format_s12_note(
    *,
    kind: str | None,
    denom: S12Denominator,
    sha: str,
) -> str:
    kind_part = f"kind={kind}; " if kind else ""
    return (
        f"{kind_part}denominator={denom.track_count} tracks; "
        f"denominator=present {denom.present}; unreachable {denom.unreachable}; "
        f"sha={sha}"
    )


def attach_s12_denominator_to_reason(reason: str, denom: S12Denominator, sha: str) -> str:
    tail = (
        f"denominator={denom.track_count} tracks; "
        f"denominator=present {denom.present}; unreachable {denom.unreachable}; "
        f"sha={sha}"
    )
    return f"{reason}; {tail}"


def s12_library_scale_rejection(kpi: str, note: str | None) -> str | None:
    if not kpi.startswith("cloudsync_"):
        return None
    haystack = note or ""
    tracks_match = _S12_TRACKS_DENOM_RE.search(haystack)
    present_match = _S12_PRESENT_DENOM_RE.search(haystack)
    if tracks_match is None or present_match is None:
        return None
    track_count = int(tracks_match.group(1))
    present = int(present_match.group(1))
    if track_count >= LIBRARY_SCALE_MIN_TRACKS and present == 0:
        return (
            f"{kpi} records {track_count} tracks with present 0; metadata-only "
            "sync is not a library-scale cohort and cannot PASS against a "
            "present ~10k baseline"
        )
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def mint_capture_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"perf-capture-{stamp}"


def session_meta(*, sha: str, now: datetime | None = None) -> CaptureMeta:
    clock = now or datetime.now(UTC)
    return CaptureMeta(
        capture_id=mint_capture_id(clock),
        date=clock.date().isoformat(),
        machine=socket.gethostname(),
        sha=sha,
    )


def git_sha(repo: Path = REPO_ROOT) -> str:
    """HEAD sha, or empty string when git cannot be read."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        return ""
    return sha


def note_with_tag(note: str) -> str:
    if CAPTURE_TAG in note:
        return note
    return f"{CAPTURE_TAG}; {note}" if note else CAPTURE_TAG


def build_row(
    *,
    kpi: str,
    value: float | None,
    unit: str,
    method: str,
    meta: CaptureMeta,
    note: str,
    status: str | None = None,
    measured: bool | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "date": meta.date,
        "round": ROUND,
        "kpi": kpi,
        "value": value,
        "unit": unit,
        "machine": meta.machine,
        "sha": meta.sha,
        "source": SOURCE,
        "method": method,
        "capture_id": meta.capture_id,
        "note": note_with_tag(note),
    }
    if status is not None:
        row["status"] = status
    if measured is not None:
        row["measured"] = measured
    return row


def error_row(
    *,
    kpi: str,
    unit: str,
    method: str,
    meta: CaptureMeta,
    reason: str,
) -> dict[str, Any]:
    return build_row(
        kpi=kpi,
        value=None,
        unit=unit,
        method=method,
        meta=meta,
        note=reason,
        status="error",
        measured=False,
    )


def withheld_row(
    *,
    kpi: str,
    unit: str,
    method: str,
    meta: CaptureMeta,
    reason: str,
) -> dict[str, Any]:
    return build_row(
        kpi=kpi,
        value=None,
        unit=unit,
        method=method,
        meta=meta,
        note=reason,
        status="withheld",
        measured=False,
    )


def required_error_rows(scenarios: Sequence[str], meta: CaptureMeta, reason: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if "S5" in scenarios:
        rows.extend(error_row(kpi=kpi, unit=S5_UNIT, method=S5_METHOD, meta=meta, reason=reason) for kpi in S5_REQUIRED)
    if "S12" in scenarios:
        rows.extend(
            error_row(kpi=kpi, unit=S12_UNIT, method=S12_METHOD, meta=meta, reason=reason) for kpi in S12_REQUIRED
        )
    return rows


def append_entries(path: Path, new_entries: Sequence[dict[str, Any]]) -> None:
    from scripts.perf.kpi_ledger_append import append_entries as splice_append

    splice_append(Path(path), list(new_entries), validate=False)


def format_appended(row: dict[str, Any]) -> str:
    kpi = str(row.get("kpi", ""))
    if row.get("status") == "error":
        return f"{kpi}  ERROR  {row.get('note', '')}"
    if row.get("status") == "withheld":
        return f"{kpi}  WITHHELD  {row.get('note', '')}"
    return f"{kpi}  {row.get('value')} {row.get('unit', '')}"


def has_error_row(rows: Sequence[dict[str, Any]]) -> bool:
    return any(row.get("status") == "error" for row in rows)


def required_numeric_present(scenarios: Sequence[str], rows: Sequence[dict[str, Any]]) -> bool:
    found: set[str] = set()
    for row in rows:
        if row.get("status") == "error":
            continue
        value = row.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        found.add(str(row.get("kpi")))
    needed: list[str] = []
    if "S5" in scenarios:
        needed.extend(S5_REQUIRED)
    if "S12" in scenarios:
        needed.extend(S12_REQUIRED)
    return all(name in found for name in needed)


def fetch_url(url: str, timeout: float) -> tuple[int, Any, bytes]:
    """GET ``url``, reading the full body. Does not follow 3xx."""
    request = Request(url, method="GET")
    try:
        with _OPENER.open(request, timeout=timeout) as resp:
            return int(resp.status), resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp is not None else b""
        return int(exc.code), exc.headers, body
