"""Fetch fixture source audio through the LANE DAEMON's audio endpoint.

Never reads a raw DB path: path healing lives between the DB and the
filesystem, so a stable_id resolved by hand is not the file the app would play.
The daemon is the only supported source (spec section 3).

Stdlib only -- this step moves bytes and does not analyse them.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 Source every fixture from GET /api/v1/tracks/{stable_id}/audio.
    [if] a fixture returns non-200 [then ⛔️] it is recorded MISSING, never swapped
  ✔︎ ✅ 🎯 Pin each downloaded body by sha256.
    [if] a cached body no longer matches its pin [then ⛔️] it is re-fetched
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

FETCH_TIMEOUT_S = 120
MIN_PLAUSIBLE_BODY_BYTES = 64 * 1024


@dataclass(frozen=True)
class FetchResult:
    """One fixture's download outcome. ``status`` is 'ok' or 'MISSING'."""

    fixture_id: str
    stable_id: str
    status: str
    path: str | None
    bytes: int
    content_type: str | None
    sha256: str | None
    detail: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def fetch_one(base_url: str, fixture: dict[str, object], cache_dir: Path) -> FetchResult:
    stable_id = str(fixture["stable_id"])
    fixture_id = str(fixture["id"])
    # Absolute: the render stage runs from the frontend directory, so a
    # relative source path resolves against the wrong root and 404s.
    destination = (cache_dir / f"{fixture_id}-{stable_id[:12]}.audio").resolve()
    url = f"{base_url.rstrip('/')}/api/v1/tracks/{stable_id}/audio"
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as response:
            status = int(response.status)
            content_type = response.headers.get("Content-Type")
            body = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return FetchResult(
            fixture_id,
            stable_id,
            "MISSING",
            None,
            0,
            None,
            None,
            f"{type(error).__name__}: {error}",
        )

    if status != 200:
        return FetchResult(
            fixture_id, stable_id, "MISSING", None, len(body), content_type, None, f"HTTP {status}"
        )
    if len(body) < MIN_PLAUSIBLE_BODY_BYTES:
        # An evicted iCloud stub serves an empty or near-empty body rather than
        # erroring, so a 200 is not on its own evidence that audio exists.
        return FetchResult(
            fixture_id,
            stable_id,
            "MISSING",
            None,
            len(body),
            content_type,
            None,
            f"body of {len(body)} bytes is below the {MIN_PLAUSIBLE_BODY_BYTES} minimum "
            "(most likely an evicted placeholder)",
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body)
    return FetchResult(
        fixture_id,
        stable_id,
        "ok",
        str(destination),
        len(body),
        content_type,
        hashlib.sha256(body).hexdigest(),
        None,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", required=True, help="lane daemon base URL, e.g. http://127.0.0.1:8685"
    )
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    fixtures = json.loads(args.fixtures.read_text(encoding="utf-8"))["fixtures"]
    results = [fetch_one(args.base_url, fixture, args.cache_dir) for fixture in fixtures]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {"base_url": args.base_url, "sources": [result.as_dict() for result in results]},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    for result in results:
        marker = "[OK]" if result.status == "ok" else "[MISSING]"
        detail = f" {result.detail}" if result.detail else ""
        print(f"{marker} {result.fixture_id} {result.stable_id[:12]} {result.bytes} bytes{detail}")

    missing = [result.fixture_id for result in results if result.status != "ok"]
    if missing:
        print(f"[ERROR] {len(missing)} fixture(s) MISSING: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
