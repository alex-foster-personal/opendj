"""Put the staged engine payload into a packed Electron app, on any host (OPS-63).

``pnpm pack:dir`` (macOS) and ``pnpm pack:dir:linux`` pack the shell with the
empty placeholder ``payload/`` directory as an ``extraResources`` entry. This
module swaps that placeholder for the payload ``scripts.build_engine_payload``
staged, so the packed app boots the bundled engine and the Rust audio engine
(``bin/odj-audio``) instead of an empty directory. It refuses a stage whose
manifest was built from any commit but the checkout's HEAD, so a stale
``out/payload-stage`` can never ship an old engine.

Why a copy step after the pack, not an electron-builder ``extraResources``
entry pointed at the stage: the payload is a relocatable CPython tree full of
symlinks (``runtime/bin/python3 -> python3.11``) and executable bits, and
``ditto`` on macOS also carries extended attributes. A copy this module owns
preserves each of those the same way on every host, and the placeholder guard
refuses to delete anything but the README a fresh pack writes, so a pack that
already holds a payload is never overwritten blind. electron-builder's own file
copier gives no such guarantee to check against, and making the config depend
on an env var would break the plain ``pack:dir`` the dev loop uses.

Copy: ``ditto`` on macOS (xattrs, as the #6117 bundle was assembled), and
``shutil.copytree(symlinks=True)`` everywhere else. After the copy, the two
entry points the shell needs must be executable files in the PACKED tree:
``bin/opendj-engine`` (engine.ts ENGINE_LAUNCHER) and ``bin/odj-audio`` (the
ODJ_AUDIO_BIN the shell exports only when that file is executable).

Usage, the whole Linux flow from a clean checkout at the commit to ship::

    pnpm --dir apps/webui/frontend install --frozen-lockfile
    pnpm --dir apps/webui/frontend run build      # the payload refuses a stale SPA
    uv run --no-sync python -m scripts.build_engine_payload \
        --out apps/desktop/electron/out/payload-stage --label ''
    pnpm --dir apps/desktop/electron install --frozen-lockfile   # tsc, electron-builder
    OPENDJ_BUILD_LANE_LABEL=electron OPENDJ_BUILD_CHANNEL=dev \
        pnpm --dir apps/desktop/electron pack:dir:linux   # stamp-build.mjs needs both
    python -m scripts.electron_payload            # stage -> this host's packed app

    python -m scripts.electron_payload --staged DIR --out apps/desktop/electron/out

Stdlib only and Python 3.9 compatible, like scripts/ship_electron.py.

-Claude
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from scripts.payload_output_probe import OUTPUT_PROBE_RELATIVE
except ImportError:  # run as a file: python -m scripts.electron_payload
    from payload_output_probe import OUTPUT_PROBE_RELATIVE  # type: ignore[import-not-found,no-redef]

REPO_ROOT = Path(__file__).resolve().parents[1]
ELECTRON_OUT = REPO_ROOT / "apps/desktop/electron/out"
APP_NAME = "Open DJ.app"
PAYLOAD_DIR = "payload"
#: The files the shell itself starts. A payload missing either is not shippable.
REQUIRED_EXECUTABLES = ("bin/opendj-engine", "bin/odj-audio")
#: What a fresh pack leaves in resources/payload: apps/desktop/electron/payload/.
PLACEHOLDER_LISTING = ["README.md"]


def required_executables(host: str) -> tuple[str, ...]:
    """Entry points the packed tree must carry for this host."""
    if host == "darwin":
        return (*REQUIRED_EXECUTABLES, OUTPUT_PROBE_RELATIVE)
    return REQUIRED_EXECUTABLES


class PayloadInstallError(RuntimeError):
    """The payload could not be put into the packed app. The message says why."""


def packed_resources_dir(out_dir: Path, host: str, machine: str) -> Path:
    """Where electron-builder ``--dir`` puts ``resources/`` for this host.

    ``host`` is ``sys.platform`` and ``machine`` is ``platform.machine()``.
    electron-builder names its unpacked dir by platform and, off x64, by arch.
    """
    arm = machine.lower() in ("arm64", "aarch64")
    if host == "darwin":
        mac = "mac-arm64" if arm else "mac"
        return out_dir / mac / APP_NAME / "Contents" / "Resources"
    if host.startswith("linux"):
        return out_dir / ("linux-arm64-unpacked" if arm else "linux-unpacked") / "resources"
    if host == "win32":
        return out_dir / ("win-arm64-unpacked" if arm else "win-unpacked") / "resources"
    raise PayloadInstallError(f"no electron-builder dir layout known for {host}/{machine}")


def missing_executables(payload: Path, host: str) -> list[str]:
    """Each required entry point that is not an executable regular file under ``payload``.

    On Windows the executable bit does not exist, so a regular file is enough.
    """
    missing = []
    for relative in required_executables(host):
        path = payload / relative
        if not path.is_file() or (host != "win32" and not os.access(path, os.X_OK)):
            missing.append(relative)
    return missing


def staged_commit(staged: Path) -> str | None:
    """The commit the staged payload says it was built from, or None if unreadable."""
    try:
        manifest = json.loads((staged / "manifest.json").read_text(encoding="utf-8"))
        sha = manifest["identity"]["git_sha_full"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return sha if isinstance(sha, str) else None


def install_payload(resources: Path, staged: Path, host: str, expected_sha: str | None = None) -> Path:
    """Replace ``resources/payload`` (the placeholder) with ``staged``. Returns the new dir.

    With ``expected_sha``, a stage built from any other commit (or with no
    readable commit) is refused before anything is deleted.
    """
    if not (staged / "manifest.json").is_file():
        raise PayloadInstallError(f"{staged} has no manifest.json; the payload build did not finish")
    if expected_sha is not None:
        built = staged_commit(staged)
        if built != expected_sha:
            raise PayloadInstallError(
                f"{staged} was built from {built or 'an unreadable commit'}, not {expected_sha}; "
                "rebuild it with scripts.build_engine_payload at this commit"
            )
    target = resources / PAYLOAD_DIR
    present = sorted(p.name for p in target.iterdir()) if target.is_dir() else None
    if present != PLACEHOLDER_LISTING:
        raise PayloadInstallError(
            f"{target} is not the empty placeholder (found {present!r}); refusing to delete it. "
            "Re-run the pack from a clean out/ directory."
        )
    absent = missing_executables(staged, host)
    if absent:
        raise PayloadInstallError(f"{staged} lacks executable {', '.join(absent)}; rebuild the payload")
    shutil.rmtree(target)
    if host == "darwin":
        ditto = shutil.which("ditto")
        if ditto is None:
            raise PayloadInstallError("ditto not found: the macOS copy keeps extended attributes with ditto")
        subprocess.run([ditto, str(staged), str(target)], check=True)
    else:
        shutil.copytree(staged, target, symlinks=True)
    absent = missing_executables(target, host)
    if absent:
        raise PayloadInstallError(f"the copy in {target} lost executable {', '.join(absent)}")
    return target


def checkout_head() -> str:
    """This checkout's HEAD, the commit the staged payload must match."""
    git = shutil.which("git")
    if git is None:
        raise PayloadInstallError("git is not on PATH, so the stage cannot be tied to this commit (UNKNOWN)")
    out = subprocess.run([git, "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise PayloadInstallError(f"git rev-parse HEAD failed (UNKNOWN): {out.stderr.strip()}")
    return out.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", type=Path, default=ELECTRON_OUT / "payload-stage")
    parser.add_argument("--out", type=Path, default=ELECTRON_OUT, help="electron-builder output dir")
    args = parser.parse_args(argv)
    try:
        head = checkout_head()
        resources = packed_resources_dir(args.out, sys.platform, platform.machine())
        target = install_payload(resources, args.staged, sys.platform, expected_sha=head)
    except (PayloadInstallError, OSError, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] payload installed at {target}")
    for relative in required_executables(sys.platform):
        print(f"[OK] executable {target / relative}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
