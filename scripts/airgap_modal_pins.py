"""Pin modal and every dependency it pulls when airgap-mac adds it to the repo venv.

airgap-mac.yml fills `.venv` with `uv sync --locked --all-extras` (uv.lock), then
adds modal, which uv.lock does not carry. A bare `uv pip install modal==X` pins
modal alone and resolves the rest (grpclib, synchronicity, h2, ...) from the
live index, so a cold cache or a new transitive release changes what the job
tests (Codex, PR #6502, P2/BLOCKING). pylock.ci.toml pins all of them.

Usage:
    python -m scripts.airgap_modal_pins constraints --installed BEFORE --lock pylock.ci.toml
    python -m scripts.airgap_modal_pins verify --before BEFORE --after AFTER --lock pylock.ci.toml

`constraints` prints one `name==version` line per package: what is already
installed keeps its uv.lock version, and every other package the CI lock names
gets the CI lock's version, so the install resolves nothing freely and cannot
move a uv.lock package. `verify` exits 1 if the install added or changed any
package that is not at the CI lock's version, so a dependency neither lock
pins fails the job instead of floating in.

Requirements (mini-PRD)
- [if] a package is installed [then] its constraint is the installed version, [else stop]
- [if] a package is not installed and the CI lock names it [then] its constraint is the lock's version, [else stop]
- [if] the install added or changed a package to a version the CI lock does not pin [then] verify exits 1 naming it, [else stop]
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;#]+)")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_freeze(text: str) -> dict[str, str]:
    """`uv pip freeze` output as {normalized name: version}; editable and URL lines are skipped."""
    pins: dict[str, str] = {}
    for line in text.splitlines():
        match = _PIN.match(line.strip())
        if match:
            pins[_norm(match.group(1))] = match.group(2)
    return pins


def lock_pins(text: str) -> dict[str, str]:
    """A PEP 751 lock's packages as {normalized name: version}."""
    return {_norm(p["name"]): str(p["version"]) for p in tomllib.loads(text).get("packages", []) if "version" in p}


def constraints(installed: dict[str, str], lock: dict[str, str]) -> list[str]:
    merged = {**lock, **installed}
    return [f"{name}=={merged[name]}" for name in sorted(merged)]


def drift(before: dict[str, str], after: dict[str, str], lock: dict[str, str]) -> list[str]:
    """Packages the install added or changed to a version the CI lock does not pin."""
    return [
        f"{name}=={version} (was {before.get(name, 'absent')}, pylock.ci.toml has {lock.get(name, 'none')})"
        for name, version in sorted(after.items())
        if before.get(name) != version and lock.get(name) != version
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("constraints")
    c.add_argument("--installed", type=Path, required=True)
    c.add_argument("--lock", type=Path, required=True)
    v = sub.add_parser("verify")
    v.add_argument("--before", type=Path, required=True)
    v.add_argument("--after", type=Path, required=True)
    v.add_argument("--lock", type=Path, required=True)
    args = parser.parse_args(argv)
    lock = lock_pins(args.lock.read_text(encoding="utf-8"))
    if not lock:
        print(f"[airgap-modal] {args.lock} names no packages", file=sys.stderr)
        return 2
    if args.cmd == "constraints":
        print("\n".join(constraints(parse_freeze(args.installed.read_text(encoding="utf-8")), lock)))
        return 0
    bad = drift(
        parse_freeze(args.before.read_text(encoding="utf-8")),
        parse_freeze(args.after.read_text(encoding="utf-8")),
        lock,
    )
    for line in bad:
        print(f"[airgap-modal] not at a locked version: {line}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
