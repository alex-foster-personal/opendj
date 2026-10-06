"""Observe DEVOPS-04 dmg-smoke cadence from Linux (row 7, issue #2352).

Does not build, mount, or launch a dmg. Linux can prove the fail-fast
instrument is intact and whether ledger issue #1492 carries a headed Mac
row-7 comment. Headed attach stays UNOBSERVED / needs:mac until that comment
exists. This observer never writes a ``row=7 sha=`` marker: that marker is
owned by ``ops/dmg-smoke/run.sh`` on the Air.

Exit codes: 0 instrument ok (mac may be UNOBSERVED), 1 instrument broken
or headed evidence is not a fresh PASS or FLAG with positive counts, 2 UNKNOWN
(ledger unreadable). A failed read is never rendered as UNOBSERVED. A
parseable headed FAIL, a purported PASS with zero tracks or playlists, or
evidence older than the 7-day clock is a failing verdict, never EXIT_OK.

    python -m scripts.dmg_smoke_cadence_observe
    python -m scripts.dmg_smoke_cadence_observe --root /path --ledger-issue 1492
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "private_owner/music-dj-tools"
DEFAULT_LEDGER_ISSUE = 1492

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_UNKNOWN = 2

HEADING = "## Periodic check row 7 (desktop dmg smoke)"
ZERO_COUNT = '[ "$TRACKS" -gt 0 ] && [ "$PLAYLISTS" -gt 0 ]'
REQUIRED_FILES = (
    Path("ops/dmg-smoke/run.sh"),
    Path("ops/dmg-smoke/com.af.dmg-smoke.plist.template"),
    Path("scripts/install_dmg_smoke_launchd.sh"),
    Path("tests/scripts/test_dmg_smoke_run.py"),
)
REQUIRED_SUBSTRINGS = (
    ZERO_COUNT,
    "library-attached",
    "/api/v1/preflight",
    "/api/v1/health",
)
SHIP_DMG_LINE = re.compile(r"(?:^|[\s;/])ship_dmg\.sh(?:\s|$)")
RESULT_RE = re.compile(r"^- result: (\S+)", re.M)
COUNTS_RE = re.compile(r"- tracks: (\d+) playlists: (\d+)")
HOST_RE = re.compile(r"^- host: (\S+)", re.M)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SUMMARY_PREFIX = "[dmg-smoke-cadence]"
CLOCK_DAYS = 7


def _gh_env() -> dict[str, str]:
    """gh honors CLICOLOR_FORCE even when stdout is captured (nucbox WSL)."""
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["CLICOLOR"] = "0"
    env.pop("CLICOLOR_FORCE", None)
    env.pop("FORCE_COLOR", None)
    env.pop("GH_FORCE_TTY", None)
    return env


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


@dataclass(frozen=True)
class HeadedRun:
    created_at: str
    result: str
    tracks: int
    playlists: int
    host: str


def instrument_findings(root: Path) -> list[str]:
    findings: list[str] = []
    for rel in REQUIRED_FILES:
        path = root / rel
        if not path.is_file():
            findings.append(f"missing {rel.as_posix()}")
    run_sh = root / "ops" / "dmg-smoke" / "run.sh"
    if not run_sh.is_file():
        return findings
    text = run_sh.read_text(encoding="utf-8")
    findings.extend(
        f"run.sh missing fail-fast {needle!r}"
        for needle in REQUIRED_SUBSTRINGS
        if needle not in text
    )
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        if SHIP_DMG_LINE.search(code):
            findings.append("run.sh invokes ship_dmg.sh")
            break
    return findings


def flatten_comments(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        raise TypeError("comments payload is not a list")
    if not raw:
        return []
    if isinstance(raw[0], list):
        out: list[dict] = []
        for page in raw:
            if not isinstance(page, list):
                raise TypeError("slurped comments page is not a list")
            out.extend(page)
        return out
    return raw


def parse_headed_run(comment: dict) -> HeadedRun | None:
    body = str(comment.get("body") or "")
    if HEADING not in body:
        return None
    result_m = RESULT_RE.search(body)
    counts_m = COUNTS_RE.search(body)
    if result_m is None or counts_m is None:
        return None
    host_m = HOST_RE.search(body)
    return HeadedRun(
        created_at=str(comment.get("created_at") or ""),
        result=result_m.group(1),
        tracks=int(counts_m.group(1)),
        playlists=int(counts_m.group(2)),
        host=host_m.group(1) if host_m else "unknown",
    )


def parse_created_at(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        then = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return then.astimezone(UTC)


def _parse_now_arg(value: str) -> datetime:
    parsed = parse_created_at(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"unparseable --now {value!r}")
    return parsed


def elapsed_days_since(created_at: str, now: datetime) -> int | None:
    then = parse_created_at(created_at)
    if then is None:
        return None
    current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return int((current - then).total_seconds() // 86400)


# Results whose attach held. FLAG is a slow build with a successful attach:
# spec 93839409 says it "does not by itself FAIL the row", and run.sh exits 0
# for it. Supersedes: PASS-only, which made every notarizing host (two Apple
# notarizations alone exceed the 720s budget) read mac=FAIL forever.
ATTACH_HELD_RESULTS = frozenset({"PASS", "FLAG"})


def headed_attach_held(headed: HeadedRun) -> bool:
    return (
        headed.result in ATTACH_HELD_RESULTS
        and headed.tracks > 0
        and headed.playlists > 0
    )


def headed_is_fresh(headed: HeadedRun, now: datetime, clock_days: int) -> bool:
    age = elapsed_days_since(headed.created_at, now)
    if age is None:
        return False
    return age < clock_days


def latest_headed(comments: Sequence[dict]) -> HeadedRun | None:
    found = [run for run in (parse_headed_run(c) for c in comments) if run is not None]
    if not found:
        return None

    def sort_key(run: HeadedRun) -> tuple[int, str]:
        parsed = parse_created_at(run.created_at)
        stamp = parsed.isoformat() if parsed is not None else ""
        return (1 if parsed is not None else 0, stamp)

    return max(found, key=sort_key)


def fetch_comments(repo: str, issue: int) -> list[dict]:
    proc = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repo}/issues/{issue}/comments",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=_gh_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh exit {proc.returncode}")
    try:
        raw = json.loads(strip_ansi(proc.stdout or "") or "[]")
        comments = flatten_comments(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(f"unreadable comments payload: {exc}") from exc
    return comments


def summary_line(
    *,
    findings: Sequence[str],
    headed: HeadedRun | None,
    now: datetime | None = None,
    clock_days: int = CLOCK_DAYS,
) -> str:
    if findings:
        return f"{SUMMARY_PREFIX} instrument=FAIL " + "; ".join(findings)
    if headed is None:
        return (
            f"{SUMMARY_PREFIX} instrument=ok mac=UNOBSERVED needs:mac "
            "headed_comments=0"
        )
    age = elapsed_days_since(headed.created_at, now) if now is not None else None
    stale = now is None or not headed_is_fresh(headed, now, clock_days)
    if not headed_attach_held(headed):
        mac = "FAIL"
    elif stale:
        mac = "STALE"
    else:
        mac = "OBSERVED"
    line = (
        f"{SUMMARY_PREFIX} instrument=ok mac={mac} result={headed.result} "
        f"tracks={headed.tracks} playlists={headed.playlists} "
        f"at={headed.created_at} host={headed.host}"
    )
    if stale:
        age_token = str(age) if age is not None else "unknown"
        line += f" age_days={age_token} clock_days={clock_days}"
    return line


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--ledger-issue", type=int, default=DEFAULT_LEDGER_ISSUE)
    parser.add_argument(
        "--now",
        type=_parse_now_arg,
        default=None,
        help="UTC ISO timestamp for the 7-day clock (tests). Default: now.",
    )
    parser.add_argument(
        "--clock-days",
        type=int,
        default=CLOCK_DAYS,
        help="Maximum age in whole days for headed evidence (default: 7).",
    )
    args = parser.parse_args(argv)
    now = args.now if args.now is not None else datetime.now(UTC)

    findings = instrument_findings(args.root)
    if findings:
        print(
            summary_line(
                findings=findings, headed=None, now=now, clock_days=args.clock_days
            )
        )
        return EXIT_FINDINGS

    try:
        comments = fetch_comments(args.repo, args.ledger_issue)
    except RuntimeError as exc:
        print(f"{SUMMARY_PREFIX} UNKNOWN - NOT MEASURED ({exc})", file=sys.stderr)
        print(f"{SUMMARY_PREFIX} UNKNOWN - NOT MEASURED")
        return EXIT_UNKNOWN

    headed = latest_headed(comments)
    print(
        summary_line(
            findings=(), headed=headed, now=now, clock_days=args.clock_days
        )
    )
    if headed is None:
        return EXIT_OK
    if headed_attach_held(headed) and headed_is_fresh(headed, now, args.clock_days):
        return EXIT_OK
    return EXIT_FINDINGS


if __name__ == "__main__":
    raise SystemExit(main())
