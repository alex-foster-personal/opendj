"""Semver guard for ``just release``.

Every public release must carry a strictly higher semver than the latest
published release on the updater host. Two releases sharing a number are
invisible to the Tauri updater.

Acceptance:
- [if] the configured version equals the latest published version [then] check
  exits non-zero, [else broken].
- [if] the configured version is lower than the latest published version [then]
  check exits non-zero, [else broken].
- [if] no release has been published yet [then] check passes, [else broken].
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from apps.shared.semver import Semver
from apps.shared.semver import parse_semver as _parse_semver

DEFAULT_PUBLIC_REPO = "maintainer/issue-assets"


def parse_semver(raw: str, *, source: str) -> Semver:
    """Parse a manifest version or the accepted app-v release-tag namespace."""
    candidate = raw.strip()
    if candidate.startswith("app-v"):
        candidate = candidate[4:]
    try:
        return _parse_semver(candidate)
    except ValueError as exc:
        raise ValueError(f"{source} is {raw!r}, which is not a semver version") from exc


def require_semver_bump(configured: str, published: str | None) -> None:
    """Refuse when ``configured`` is not strictly greater than ``published``."""
    here = parse_semver(configured, source="tauri.conf.json version")
    if published is None:
        return
    there = parse_semver(published, source="the latest published release")
    if here <= there:
        raise ValueError(
            f"release requires a semver bump: configured {configured!r} is not "
            f"greater than the latest published {published!r}. Bump "
            "apps/desktop/src-tauri/tauri.conf.json version before running "
            "just release."
        )


def read_configured_version(config_path: str) -> str:
    with open(config_path, encoding="utf-8") as handle:
        conf = json.load(handle)
    version = conf.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{config_path} has no string version field")
    return version.strip()


def latest_published_version(public_repo: str) -> str | None:
    proc = subprocess.run(
        [
            "gh",
            "release",
            "list",
            "--repo",
            public_repo,
            "--limit",
            "1",
            "--json",
            "tagName",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown gh error"
        raise RuntimeError(f"could not read releases from {public_repo}: {detail}")
    rows = json.loads(proc.stdout or "[]")
    if not rows:
        return None
    tag_name = rows[0].get("tagName")
    if not isinstance(tag_name, str) or not tag_name.strip():
        raise RuntimeError(f"{public_repo} returned a release without tagName")
    return tag_name.strip()


def check_release_semver(*, config_path: str, public_repo: str) -> str:
    configured = read_configured_version(config_path)
    published = latest_published_version(public_repo)
    require_semver_bump(configured, published)
    if published is None:
        return configured
    return configured


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.release_semver",
        description="Refuse just release when tauri.conf.json has no semver bump.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="compare configured and published versions")
    check.add_argument(
        "--config",
        default="apps/desktop/src-tauri/tauri.conf.json",
        help="path to tauri.conf.json",
    )
    check.add_argument(
        "--repo",
        default=DEFAULT_PUBLIC_REPO,
        help="public release repository that hosts latest.json",
    )
    check.add_argument(
        "--configured",
        default=None,
        help="override configured version (tests only)",
    )
    check.add_argument(
        "--published",
        default=None,
        help="override latest published version (tests only; empty means none)",
    )
    args = parser.parse_args(argv)

    configured = args.configured if args.configured is not None else read_configured_version(args.config)
    if args.published is None:
        published = latest_published_version(args.repo)
    elif args.published == "":
        published = None
    else:
        published = args.published

    try:
        require_semver_bump(configured, published)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if published is None:
        print(f"[OK] first release: configured version is {configured!r}")
    else:
        print(f"[OK] semver bump: configured {configured!r} > published {published!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
