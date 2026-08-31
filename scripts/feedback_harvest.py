"""Harvest in-app feedback from running Open DJ daemons (FB-06).

Usage::

    just feedback-harvest              # harvest local + silver, live
    just feedback-harvest -- --dry-run # print only, archive nothing
    just feedback-harvest -- --url http://127.0.0.1:8690  # extra target

What it does, per reachable daemon:

1. GET /api/v1/feedback/{todos,comments,general} and print the full snapshot.
2. Append every HARVESTABLE item (done todos, per-item feedback text,
   comments, a non-empty general note) to zTasks.md with provenance
   (machine, the item's own build sha, timestamp).
3. POST /api/v1/feedback/archive so the server moves those items to
   feedback/archive-<date>.json. Nothing is ever deleted; a second run
   therefore appends no duplicates.

Append happens BEFORE archive on purpose: if the archive call fails the worst
case is a duplicate zTasks line on the retry, never a lost item.

Target discovery (documented per the FB-06 brief):

- Local dev daemons: $MUSIC_DJ_BACKEND_PORT (this worktree's claimed port,
  from .env) and the default 8585, health-checked before use.
- Ad hoc audition servers: every 127.0.0.1 listener, filtered to those that
  actually answer /api/v1/feedback/todos. Agents open these on arbitrary
  ports to put a build in front of the maintainer, so his feedback lands there.
- Installed apps (local Mac AND silver over ssh): the ship-dmg probe, reused
  verbatim from .agents/skills/ship-dmg/scripts/ship_dmg.sh - pgrep the
  bundled engine python, then lsof its listening TCP port. The engine binds
  an OS-assigned ephemeral port (apps/desktop/src-tauri/src/engine.rs), so
  the live socket is the only truth; nothing hardcoded.

Requirements (mini-PRD):
- ✔︎ ✅ every reachable daemon's feedback is printed and appended with
  provenance. [if] a harvested item lacks machine/sha/timestamp [then ⛔️]
- ✔︎ ✅ harvested items are archived server-side, never deleted.
  [if] a second immediate run appends the same item again [then ⛔️]
- ✔︎ ✅ zero reachable daemons is an explicit exit 1, and an unreachable
  silver is a loud [WARN], never silence. [if] silver down looks like
  "no feedback" [then ⛔️]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
ZTASKS: Path = REPO_ROOT / "zTasks.md"
SILVER_HOST: str = "silver"
DEFAULT_DEV_PORT: int = 8585
TIMEOUT_S: int = 6

# Reused verbatim from .agents/skills/ship-dmg/scripts/ship_dmg.sh: the
# bundled engine's listening socket via lsof on its pid. The [3] bracket
# defeats pgrep -f matching its own command line.
PORT_PROBE: str = (
    'pid=$(pgrep -f "Resources/payload/runtime/bin/python[3]" | head -1); '
    'if [ -n "$pid" ]; then lsof -a -p "$pid" -iTCP -sTCP:LISTEN -nP -Fn '
    '2>/dev/null | sed -n "s/^n.*:\\([0-9][0-9]*\\)$/\\1/p" | head -1; fi'
)


# ----- targets ------------------------------------------------------------
@dataclass(frozen=True)
class Target:
    """One reachable feedback API: local HTTP or curl-over-ssh."""

    machine: str
    base: str  # http://127.0.0.1:<port>
    ssh_host: str | None  # None = local

    def request(self, method: str, path: str) -> dict:
        url = f"{self.base}{path}"
        if self.ssh_host is None:
            req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        out = subprocess.run(
            ["ssh", self.ssh_host, f"curl -sf -m {TIMEOUT_S} -X {method} {url}"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S * 5,
            check=True,
        )
        return json.loads(out.stdout)


# Ad hoc audition servers: agents spin these up on arbitrary local ports to
# put a build in front of the maintainer, and that is exactly where he leaves review
# feedback. Discovery previously covered only the .env port, 8585 and the
# installed app, so a pin left on an audition port sat unharvested (his
# 15:07Z /performance pin on port 8693, found an hour late on Mon 31 Aug
# 2026). Every 127.0.0.1 listener is a candidate; _serves_feedback filters.
LOCAL_LISTENER_PROBE: str = (
    "lsof -nP -iTCP@127.0.0.1 -sTCP:LISTEN -Fn 2>/dev/null | "
    'sed -n "s/^n.*:\\([0-9][0-9]*\\)$/\\1/p" | sort -u'
)


def _local_listening_ports() -> list[int]:
    """Every port bound on 127.0.0.1 right now, ascending. Empty on failure."""
    try:
        out = subprocess.run(
            ["bash", "-c", LOCAL_LISTENER_PROBE],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[WARN] local listener scan failed: {exc}")
        return []
    return sorted(
        {int(line) for line in out.stdout.split() if re.fullmatch(r"\d+", line)}
    )


def _probe_installed_port(ssh_host: str | None) -> int | None:
    argv = (
        ["bash", "-c", PORT_PROBE]
        if ssh_host is None
        else ["ssh", ssh_host, PORT_PROBE]
    )
    try:
        out = subprocess.run(
            argv, capture_output=True, text=True, timeout=TIMEOUT_S * 5, check=False
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[WARN] port probe on {ssh_host or 'local'} failed: {exc}")
        return None
    port = out.stdout.strip()
    return int(port) if re.fullmatch(r"\d+", port) else None


def _serves_feedback(target: Target, quiet: bool = False) -> bool:
    """Does this target answer the feedback API?

    quiet=True is for SPECULATIVE probes of scanned ports, where most
    listeners are unrelated local services (databases, proxies, other apps)
    and a warning per miss would be noise that trains the reader to ignore
    warnings. Configured targets stay loud: a dev port or an explicit --url
    that does not answer is a real problem worth surfacing.
    """
    try:
        target.request("GET", "/api/v1/feedback/todos")
    except Exception as exc:
        if not quiet:
            print(
                f"[WARN] {target.machine} {target.base} has no reachable "
                f"feedback API: {exc}"
            )
        return False
    else:
        return True


def _discover(extra_urls: list[str], skip_silver: bool) -> list[Target]:
    local = socket.gethostname().split(".")[0]
    candidates: list[Target] = []

    seen_ports: set[int] = set()
    for raw in [*_env_ports(), DEFAULT_DEV_PORT]:
        if raw in seen_ports:
            continue
        seen_ports.add(raw)
        candidates.append(
            Target(machine=f"{local}-dev", base=f"http://127.0.0.1:{raw}", ssh_host=None)
        )

    installed = _probe_installed_port(None)
    if installed is not None and installed not in seen_ports:
        candidates.append(
            Target(
                machine=f"{local}-app",
                base=f"http://127.0.0.1:{installed}",
                ssh_host=None,
            )
        )

    for port in _local_listening_ports():
        if port in seen_ports:
            continue
        seen_ports.add(port)
        scanned = Target(
            machine=f"{local}-audition",
            base=f"http://127.0.0.1:{port}",
            ssh_host=None,
        )
        if _serves_feedback(scanned, quiet=True):
            candidates.append(scanned)

    candidates.extend(
        Target(machine=f"{local}-url", base=url.rstrip("/"), ssh_host=None)
        for url in extra_urls
    )

    if not skip_silver:
        silver_port = _probe_installed_port(SILVER_HOST)
        if silver_port is None:
            print(
                "[WARN] silver: no running Open DJ engine found over ssh "
                "(machine asleep, app closed, or ssh down) - skipping silver"
            )
        else:
            candidates.append(
                Target(
                    machine="silver-app",
                    base=f"http://127.0.0.1:{silver_port}",
                    ssh_host=SILVER_HOST,
                )
            )

    return [t for t in candidates if _serves_feedback(t)]


def _env_ports() -> list[int]:
    env_file = REPO_ROOT / ".env"
    ports: list[int] = []
    raw = os.environ.get("MUSIC_DJ_BACKEND_PORT")
    if raw and raw.isdigit():
        ports.append(int(raw))
    if env_file.is_file():
        match = re.search(
            r"^MUSIC_DJ_BACKEND_PORT=(\d+)$", env_file.read_text(), re.MULTILINE
        )
        if match:
            ports.append(int(match.group(1)))
    return ports


# ----- harvest ------------------------------------------------------------
def _sha(item: dict) -> str:
    build = item.get("build") or {}
    return build.get("git_sha") or f"build?({build.get('error', 'unstamped')})"


def _harvest_lines(target: Target) -> tuple[list[str], dict]:
    """(zTasks lines for everything harvestable, full snapshot for printing)."""
    todos = target.request("GET", "/api/v1/feedback/todos")["todos"]
    comments = target.request("GET", "/api/v1/feedback/comments")["comments"]
    general = target.request("GET", "/api/v1/feedback/general")

    lines: list[str] = []
    for todo in todos:
        tag = f"[{target.machine} {_sha(todo)} {todo['updated_at']}]"
        if todo["done"]:
            chosen = f" chose {todo['chosen_option']!r}" if todo["chosen_option"] else ""
            fb = f" - feedback: {todo['feedback']}" if todo["feedback"] else ""
            lines.append(
                f"- [ ] feedback(review-done) {tag}: \"{todo['title']}\"{chosen}{fb}"
            )
        elif todo["feedback"]:
            lines.append(
                f"- [ ] feedback(open-todo) {tag}: \"{todo['title']}\" - {todo['feedback']}"
            )
    for pin in comments:
        anchor = f" near {pin['anchor']}" if pin["anchor"] else ""
        lines.append(
            f"- [ ] feedback(pin) [{target.machine} {_sha(pin)} {pin['created_at']}]: "
            f"{pin['text']} (at {pin['x_pct']}%,{pin['y_pct']}% on {pin['page']}{anchor})"
        )
    if general["text"]:
        stamp = general.get("updated_at") or "?"
        lines.append(
            f"- [ ] feedback(general) [{target.machine} {_sha(general)} {stamp}]: "
            f"{general['text']}"
        )
    snapshot = {"todos": todos, "comments": comments, "general": general}
    return lines, snapshot


def _print_snapshot(target: Target, snapshot: dict) -> None:
    print(f"\n=== {target.machine} ({target.base}) ===")
    todos = snapshot["todos"]
    print(f"review todos: {len(todos)} ({sum(1 for t in todos if not t['done'])} open)")
    for todo in todos:
        mark = "x" if todo["done"] else " "
        extra = f"  feedback: {todo['feedback']}" if todo["feedback"] else ""
        print(f"  [{mark}] {todo['title']}{extra}")
    print(f"comment pins: {len(snapshot['comments'])}")
    for pin in snapshot["comments"]:
        print(f"  - {pin['text']} ({pin['page']} {pin['x_pct']}%,{pin['y_pct']}%)")
    general = snapshot["general"]
    print(f"general note: {general['text']!r}" if general["text"] else "general note: (empty)")


def _append_ztasks(all_lines: list[str]) -> None:
    stamp = datetime.now(UTC).strftime("%a %d %b %Y %H:%M UTC")
    block = f"\n## In-app feedback harvest {stamp}\n" + "\n".join(all_lines) + "\n"
    with ZTASKS.open("a", encoding="utf-8") as fh:
        fh.write(block)
    print(f"\n[OK] appended {len(all_lines)} item(s) to {ZTASKS}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="print only; no zTasks append, no archive"
    )
    parser.add_argument(
        "--url", action="append", default=[], help="extra base URL to harvest"
    )
    parser.add_argument(
        "--skip-silver", action="store_true", help="do not reach for silver over ssh"
    )
    args = parser.parse_args(argv)

    targets = _discover(args.url, args.skip_silver)
    if not targets:
        print("[ERROR] no reachable feedback API on any target; nothing harvested")
        return 1

    all_lines: list[str] = []
    for target in targets:
        lines, snapshot = _harvest_lines(target)
        _print_snapshot(target, snapshot)
        all_lines.extend(lines)

    if not all_lines:
        print("\n[OK] nothing new to harvest; zTasks.md untouched, no archive written")
        return 0
    if args.dry_run:
        print("\n[OK] dry run - would append:")
        print("\n".join(all_lines))
        return 0

    _append_ztasks(all_lines)
    failures = 0
    for target in targets:
        try:
            result = target.request("POST", "/api/v1/feedback/archive")
        except (
            urllib.error.URLError,
            subprocess.SubprocessError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            failures += 1
            print(
                f"[ERROR] archive on {target.machine} failed: {exc} - items stay "
                "active there; RE-RUN harvest (duplicates in zTasks.md are "
                "possible, loss is not)"
            )
            continue
        if result["archived_to"]:
            print(
                f"[OK] {target.machine}: archived to {result['archived_to']} "
                f"(todos {result['todos_archived']}, feedback "
                f"{result['todo_feedback_archived']}, pins {result['comments_archived']}, "
                f"general {result['general_archived']})"
            )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
