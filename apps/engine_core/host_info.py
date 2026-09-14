"""``GET /api/v1/host-info`` -- measured cpu/ram from psutil.

Resolved once at engine construction, like build-info. Never guesses when
psutil cannot answer.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from apps.shared.perf_tier import HostInfoUnavailable, read_host_facts

HOST_INFO_PATH: str = "/api/v1/host-info"
CODE_HOST_INFO_UNAVAILABLE: str = "host_info_unavailable"
HOST_INFO_STATE_ATTR: str = "host_info"

CANARY_SCHEMA: int = 1
CANARY_ITERATIONS: int = 5_000
CANARY_SEED: bytes = b"perf-tier-canary-v1-opendj-532"
CANARY_FILENAME: str = "perf-tier-canary.json"


class HostInfoOut(BaseModel):
    logical_cpus: int
    ram_bytes: int
    ram_gib: float
    canary_hashes_per_second: int | None = None
    canary_elapsed_ms: float | None = None
    canary_error: str | None = None


@dataclass(frozen=True)
class HostIdentity:
    """One resolution attempt, cached for the process lifetime."""

    info: HostInfoOut | None
    failure: str | None
    logical_cpus: int | None = None
    ram_bytes: int | None = None

    def __post_init__(self) -> None:
        if (self.info is None) == (self.failure is None):
            raise ValueError(
                "HostIdentity carries either HostInfoOut or the failure reason, "
                f"never both and never neither (info={self.info!r}, failure={self.failure!r})"
            )

    def require(self) -> HostInfoOut:
        if self.info is None:
            raise HostInfoUnavailable(self.failure)
        return self.info


def _canary_path(data_dir: Path) -> Path:
    return data_dir / "state" / CANARY_FILENAME


def _run_canary_kernel() -> tuple[int, float]:
    """Bounded sha256 loop. Returns (hashes_per_second, elapsed_ms)."""
    start = time.perf_counter()
    digest = CANARY_SEED
    for _ in range(CANARY_ITERATIONS):
        digest = hashlib.sha256(digest).digest()
    elapsed = time.perf_counter() - start
    if elapsed <= 0:
        raise HostInfoUnavailable("canary clock did not advance")
    hashes_per_second = int(CANARY_ITERATIONS / elapsed)
    elapsed_ms = elapsed * 1000.0
    return hashes_per_second, elapsed_ms


def run_canary(data_dir: Path) -> tuple[int, float]:
    """Read cached canary or run once and persist."""
    path = _canary_path(data_dir)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HostInfoUnavailable(f"{path} unreadable: {exc}") from exc
        if raw.get("schema") != CANARY_SCHEMA:
            raise HostInfoUnavailable(f"{path} schema mismatch")
        hps = raw.get("hashes_per_second")
        elapsed_ms = raw.get("elapsed_ms")
        if not isinstance(hps, int) or not isinstance(elapsed_ms, (int, float)):
            raise HostInfoUnavailable(f"{path} missing canary numbers")
        return hps, float(elapsed_ms)

    hashes_per_second, elapsed_ms = _run_canary_kernel()
    measured_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "schema": CANARY_SCHEMA,
        "hashes_per_second": hashes_per_second,
        "elapsed_ms": elapsed_ms,
        "measured_at": measured_at,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return hashes_per_second, elapsed_ms


def resolve_host_info(data_dir: Path) -> HostInfoOut:
    logical_cpus, ram_bytes = read_host_facts()
    ram_gib = ram_bytes / (1024**3)
    canary_hps: int | None = None
    canary_elapsed_ms: float | None = None
    canary_error: str | None = None
    try:
        canary_hps, canary_elapsed_ms = run_canary(data_dir)
    except HostInfoUnavailable as exc:
        canary_error = str(exc)
    return HostInfoOut(
        logical_cpus=logical_cpus,
        ram_bytes=ram_bytes,
        ram_gib=ram_gib,
        canary_hashes_per_second=canary_hps,
        canary_elapsed_ms=canary_elapsed_ms,
        canary_error=canary_error,
    )


def add_host_info_route(app: FastAPI, *, data_dir: Path) -> None:
    """Resolve once at construction; serve the same answer for the process."""
    logical_cpus: int | None = None
    ram_bytes: int | None = None
    try:
        resolved = resolve_host_info(data_dir)
        failure: str | None = None
        logical_cpus = resolved.logical_cpus
        ram_bytes = resolved.ram_bytes
    except HostInfoUnavailable as exc:
        resolved, failure = None, str(exc)

    setattr(
        app.state,
        HOST_INFO_STATE_ATTR,
        HostIdentity(
            info=resolved,
            failure=failure,
            logical_cpus=logical_cpus,
            ram_bytes=ram_bytes,
        ),
    )

    @app.get(
        HOST_INFO_PATH,
        response_model=HostInfoOut,
        tags=["health"],
        name="host_info",
        responses={
            status.HTTP_503_SERVICE_UNAVAILABLE: {
                "description": "host facts could not be measured"
            }
        },
    )
    def host_info() -> HostInfoOut | JSONResponse:
        if resolved is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "error": CODE_HOST_INFO_UNAVAILABLE,
                    "message": failure,
                    "details": None,
                },
            )
        return resolved


def main(argv: list[str] | None = None) -> int:
    _ = argv
    from apps.shared.paths import DATA_DIR

    try:
        info = resolve_host_info(DATA_DIR)
    except HostInfoUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(info.model_dump(exclude_none=True), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
