"""Privacy-safe local diagnostics bundle for operators and the conn-dot bug flow.

Writes under ``.tmp/`` only; never auto-sends. Refuses while any deck is playing
or audible (same rule as telemetry). Text payload is capped at 256 KiB UTF-8.

Usage::

    uv run python -m scripts.bug_report_bundle
    just bug-report
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.shared.telemetry.live import mirror_decks_live, mirror_transport_live
from apps.shared.telemetry.scrub import (
    BUG_REPORT_TEXT_MAX_BYTES,
    assert_bug_report_text_safe,
    scrub_bug_report_mapping,
    truncate_bug_report_text,
)
from apps.shared.telemetry.sink import current_build_sha
from apps.webui.server.routes.client_errors import bug_report_client_error_row
from apps.webui.server.routes.health import bug_report_health_summary

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".tmp"
DEFAULT_DEV_PORT = 8585
TIMEOUT_S = 6
_ENV_PORT_RE = re.compile(r"^MUSIC_DJ_(BACKEND|FRONTEND)_PORT=(\d+)$", re.MULTILINE)
_BOOL_PREF_KEYS = frozenset(
    {
        "midi_enabled",
        "theme_dark",
        "telemetry_consent_accepted",
        "google_oauth_enabled",
        "deck_autocue",
    }
)


@dataclass(frozen=True)
class Ports:
    backend: int | None
    frontend: int | None


@dataclass(frozen=True)
class BackendProbe:
    base: str
    reachable: bool
    decks_live: bool
    build_sha: str
    health_summary: dict[str, object] | None
    client_errors: list[dict[str, object]]
    conn_dots: dict[str, str]


def _read_env_ports(env_path: Path) -> Ports:
    backend: int | None = None
    frontend: int | None = None
    if not env_path.is_file():
        return Ports(backend=backend, frontend=frontend)
    for match in _ENV_PORT_RE.finditer(env_path.read_text(encoding="utf-8")):
        port = int(match.group(2))
        if match.group(1) == "BACKEND":
            backend = port
        else:
            frontend = port
    return Ports(backend=backend, frontend=frontend)


def _git_sha() -> str:
    """Return HEAD via a resolved git path, else the build identity fallback."""
    git = shutil.which("git")
    if git is None:
        return current_build_sha()
    try:
        out = subprocess.run(
            [git, "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return current_build_sha()
    sha = out.stdout.strip()
    return sha or current_build_sha()


def _http_json(url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError):
        return None


def _derive_conn_dots(health: dict[str, object] | None, mirror: dict[str, Any] | None) -> dict[str, str]:
    if health is None:
        return {
            "conn_dot_fe": "unknown",
            "conn_dot_be": "offline",
            "conn_dot_lib": "unknown",
        }
    ui_url = health.get("ui_url")
    fe = "online" if isinstance(ui_url, str) and ui_url else "offline"
    be = "online" if health.get("status") == "ok" else "degraded"
    tracks = 0
    state_db = health.get("state_db")
    if isinstance(state_db, dict):
        raw_tracks = state_db.get("tracks")
        if isinstance(raw_tracks, int):
            tracks = raw_tracks
    lib = "ready" if tracks > 0 else "empty"
    if isinstance(mirror, dict) and mirror.get("client_open") is False:
        fe = "offline"
    return {
        "conn_dot_fe": fe,
        "conn_dot_be": be,
        "conn_dot_lib": lib,
    }


def _probe_backend(port: int) -> BackendProbe:
    base = f"http://127.0.0.1:{port}"
    health_raw = _http_json(f"{base}/api/v1/health")
    mirror = _http_json(f"{base}/api/v1/state/ui-mirror")
    reachable = health_raw is not None
    decks_live = False
    if isinstance(mirror, dict) and mirror.get("client_open") is not False:
        decks_live = mirror_transport_live(mirror) or mirror_decks_live(mirror)
    build_info = _http_json(f"{base}/api/v1/build-info") or {}
    build_sha = str(build_info.get("sha") or current_build_sha())
    health_summary: dict[str, object] | None = None
    if isinstance(health_raw, dict):
        try:
            from apps.webui.server.models import HealthOut

            health_summary = bug_report_health_summary(HealthOut.model_validate(health_raw))
        except Exception:
            health_summary = None
    errors_raw = _http_json(f"{base}/api/v1/client-errors?limit=20") or []
    client_errors: list[dict[str, object]] = [
        bug_report_client_error_row(row)
        for row in (errors_raw[-20:] if isinstance(errors_raw, list) else [])
        if isinstance(row, dict)
    ]
    conn_dots = _derive_conn_dots(health_summary, mirror if isinstance(mirror, dict) else None)
    return BackendProbe(
        base=base,
        reachable=reachable,
        decks_live=decks_live,
        build_sha=build_sha,
        health_summary=health_summary,
        client_errors=client_errors,
        conn_dots=conn_dots,
    )


def _read_redacted_prefs(data_dir: Path) -> dict[str, bool]:
    prefs_path = data_dir / "state" / "ui-prefs.json"
    if not prefs_path.is_file():
        return {}
    try:
        raw = json.loads(prefs_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, bool] = {}
    for key, value in raw.items():
        name = str(key)
        if name.lower() in _BOOL_PREF_KEYS and isinstance(value, bool):
            out[name] = value
    return out


def _build_payload(
    *,
    ports: Ports,
    git_sha: str,
    probe: BackendProbe | None,
    prefs_booleans: dict[str, bool],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "git_sha": git_sha,
        "build_sha": probe.build_sha if probe is not None else git_sha,
        "backend_port": ports.backend,
        "frontend_port": ports.frontend,
        "decks_busy": probe.decks_live if probe is not None else False,
        "prefs_booleans": prefs_booleans,
    }
    if probe is not None:
        payload["conn_dots"] = probe.conn_dots
        payload["health"] = probe.health_summary
        payload["client_errors"] = probe.client_errors
        payload["engine_reachable"] = probe.reachable
    else:
        payload["engine_reachable"] = False
    return scrub_bug_report_mapping(payload)


def _write_refusal(out_dir: Path, reason: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"bug-report-refused-{stamp}.json"
    body = json.dumps(
        scrub_bug_report_mapping({"schema": 1, "refusal": True, "reason": reason}),
        indent=2,
        sort_keys=True,
    )
    assert len(body.encode("utf-8")) <= BUG_REPORT_TEXT_MAX_BYTES
    path.write_text(body + "\n", encoding="utf-8")
    return path


def _write_bundle(out_dir: Path, payload: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"bug-report-{stamp}.json"
    text = json.dumps(payload, indent=2, sort_keys=True)
    text = truncate_bug_report_text(text)
    assert_bug_report_text_safe(text)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a privacy-safe diagnostics bundle under .tmp/")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("MDT_DATA_DIR", REPO_ROOT / "data")))
    parser.add_argument("--backend-port", type=int, default=None)
    args = parser.parse_args(argv)

    env_ports = _read_env_ports(REPO_ROOT / ".env")
    backend_port = args.backend_port or env_ports.backend or DEFAULT_DEV_PORT
    ports = Ports(backend=backend_port, frontend=env_ports.frontend)
    git_sha = _git_sha()
    probe = _probe_backend(backend_port)
    prefs = _read_redacted_prefs(args.data_dir)

    if probe.decks_live:
        path = _write_refusal(args.output_dir, "decks_playing_or_audible")
        try:
            shown = path.relative_to(REPO_ROOT)
        except ValueError:
            shown = path
        print(f"refused: decks live; wrote {shown}", file=sys.stderr)
        return 2

    payload = _build_payload(ports=ports, git_sha=git_sha, probe=probe, prefs_booleans=prefs)
    bundle_path = _write_bundle(args.output_dir, payload)
    print(bundle_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
