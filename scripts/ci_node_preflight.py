#!/usr/bin/env python3
"""Probe frontend ``node_modules`` public-layer symlinks before ``pnpm install``.

WHY: issue #3153 and follow-up #8987 — reused runner checkouts can keep symlinks
that dangle or point outside the tree; ``pnpm install --frozen-lockfile`` may
short-circuit ``Already up to date`` and leave the typing / vite gates broken.

WHAT: stdlib-only probe of the public layer (unscoped packages, scoped
``@scope/pkg``, and ``.bin`` entries) plus one repair attempt
(``pnpm install --frozen-lockfile``). Invoked as a file path by
``scripts/ci_node_preflight.sh`` so it runs on the runner's system Python before
``actions/setup-python`` on the frontend job.

Do not ``import`` project packages — only stdlib.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

WORKSPACE_STATE = ".pnpm-workspace-state-v1.json"


def normalize_target(raw: str) -> str:
    return raw.replace("\\", "/")


def is_unscoped_public_target(target: str) -> bool:
    """``node_modules/<pkg>`` (and ``.bin`` when it is a symlink): ``.pnpm/...`` with no ``/../``."""
    t = normalize_target(target)
    if not t.startswith(".pnpm/"):
        return False
    return "/../" not in t


def is_scoped_public_target(target: str) -> bool:
    """``node_modules/@scope/pkg``: ``../.pnpm/...`` with no ``/../`` after the first ``../``."""
    t = normalize_target(target)
    if not t.startswith("../.pnpm/"):
        return False
    rest = t[3:]  # strip first "../"
    return "/../" not in rest


def is_bin_entry_target(target: str) -> bool:
    """``node_modules/.bin/<name>``: ``../...`` but not ``../../...``; no ``/../`` in the tail."""
    t = normalize_target(target)
    if not t.startswith("../") or t.startswith("../../"):
        return False
    tail = t[3:]  # after "../"
    return "/../" not in tail


def symlink_target_shape_ok(link: Path, *, kind: str) -> bool:
    target = normalize_target(str(link.readlink()))
    if kind == "public":
        return is_unscoped_public_target(target)
    if kind == "scoped":
        return is_scoped_public_target(target)
    if kind == "bin":
        return is_bin_entry_target(target)
    raise ValueError(f"unknown kind {kind!r}")


def _relative_to_node_modules(nm: Path, link: Path) -> str:
    return link.relative_to(nm).as_posix()


def _sorted_children(path: Path) -> list[Path]:
    try:
        return sorted(path.iterdir(), key=lambda p: p.name)
    except OSError:
        return []


def _scoped_public_symlinks(scope_dir: Path) -> list[tuple[Path, str]]:
    return [
        (entry, "scoped")
        for entry in _sorted_children(scope_dir)
        if entry.is_symlink()
    ]


def _bin_entry_symlinks(bin_dir: Path) -> list[tuple[Path, str]]:
    return [
        (entry, "bin")
        for entry in _sorted_children(bin_dir)
        if entry.is_symlink()
    ]


def iter_public_layer_symlinks(nm: Path) -> list[tuple[Path, str]]:
    """Collect ``(path, kind)`` for public-layer symlinks to check.

    ``kind`` is ``public`` (unscoped or ``.bin`` dir parent as symlink),
    ``scoped`` (under ``@scope``), or ``bin`` (under ``.bin`` directory).
    """
    out: list[tuple[Path, str]] = []
    for child in _sorted_children(nm):
        if child.name in (".pnpm", ".bin"):
            continue
        if child.is_symlink():
            out.append((child, "public"))
        # Python < 3.12: Path.is_dir has no follow_symlinks=; use non-dangling dir only.
        elif child.name.startswith("@") and child.is_dir():
            out.extend(_scoped_public_symlinks(child))

    bin_path = nm / ".bin"
    if bin_path.is_symlink():
        out.append((bin_path, "public"))
    elif bin_path.is_dir():
        out.extend(_bin_entry_symlinks(bin_path))

    return out


def link_is_poisoned(link: Path, *, kind: str) -> bool:
    if not link.exists():
        return True
    if not link.is_symlink():
        return False
    return not symlink_target_shape_ok(link, kind=kind)


def sentinel_failures(nm: Path) -> list[str]:
    msgs: list[str] = []
    ts = nm / "typescript"
    if not ts.exists():
        msgs.append("missing resolvable node_modules/typescript")
    vite = nm / ".bin" / "vite"
    vite_cmd = nm / ".bin" / "vite.cmd"
    if not vite.exists() and not vite_cmd.exists():
        msgs.append("missing node_modules/.bin/vite")
    return msgs


def analyze(nm: Path) -> tuple[bool, list[str], list[Path]]:
    """Return (healthy, log lines, symlinks to unlink when repairing)."""
    log_lines: list[str] = []
    to_unlink: list[Path] = []
    for link, kind in iter_public_layer_symlinks(nm):
        if link_is_poisoned(link, kind=kind):
            log_lines.append(f"poisoned link: {_relative_to_node_modules(nm, link)}")
            to_unlink.append(link)
    log_lines.extend(sentinel_failures(nm))
    healthy = not log_lines
    return healthy, log_lines, to_unlink


def repair(
    frontend_dir: Path,
    *,
    runner: str,
    poisoned_links: list[Path],
) -> int:
    print(
        f"[ci-node-preflight] runner {runner}: frontend node_modules is poisoned; repairing once",
        file=sys.stderr,
    )
    for link in poisoned_links:
        try:
            link.unlink(missing_ok=True)
        except OSError:
            print(
                f"::error::[ci-node-preflight] runner {runner}: could not unlink {link}",
                file=sys.stderr,
            )
            return 1
    ws = frontend_dir / "node_modules" / WORKSPACE_STATE
    try:
        ws.unlink(missing_ok=True)
    except OSError:
        print(
            f"::error::[ci-node-preflight] runner {runner}: could not unlink workspace state",
            file=sys.stderr,
        )
        return 1

    pnpm_path = shutil.which("pnpm")
    if not pnpm_path:
        print(
            "::error::[ci-node-preflight] runner "
            f"{runner}: frontend node_modules is poisoned and pnpm is not on PATH",
            file=sys.stderr,
        )
        return 1

    proc = subprocess.run(
        [pnpm_path, "install", "--frozen-lockfile"],
        cwd=str(frontend_dir),
        check=False,
    )
    if proc.returncode != 0:
        print(
            "::error::[ci-node-preflight] runner "
            f"{runner}: frontend node_modules is still poisoned after reinstall",
            file=sys.stderr,
        )
        return 1

    nm = frontend_dir / "node_modules"
    ok, again, _ = analyze(nm)
    if not ok:
        for line in again:
            if line.startswith("poisoned link:") or "missing" in line:
                print(f"[ci-node-preflight] {line}", file=sys.stderr)
        print(
            "::error::[ci-node-preflight] runner "
            f"{runner}: frontend node_modules is still poisoned after reinstall",
            file=sys.stderr,
        )
        return 1

    print(f"[ci-node-preflight] runner {runner}: repaired frontend node_modules")
    return 0


def run(frontend_dir: Path, *, runner: str) -> int:
    nm = frontend_dir / "node_modules"
    if not nm.is_dir():
        return 0

    ok, problems, to_unlink = analyze(nm)
    if ok:
        return 0

    for line in problems:
        print(f"[ci-node-preflight] {line}", file=sys.stderr)

    return repair(frontend_dir, runner=runner, poisoned_links=to_unlink)


def default_runner(cli_runner: str | None) -> str:
    if cli_runner:
        return cli_runner
    env = os.environ.get("RUNNER_NAME")
    if env:
        return env
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe frontend node_modules before pnpm install.")
    parser.add_argument("--frontend-dir", required=True, type=Path, help="apps/webui/frontend")
    parser.add_argument("--runner", default=None, help="runner name (default: RUNNER_NAME or hostname)")
    ns = parser.parse_args(argv)
    runner = default_runner(ns.runner)
    try:
        return run(ns.frontend_dir.resolve(), runner=runner)
    except OSError as exc:
        print(f"::error::[ci-node-preflight] runner {runner}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
