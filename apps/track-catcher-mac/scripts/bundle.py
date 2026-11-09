#!/usr/bin/env python3
"""Build "Open DJ Track Catcher.app": engine, Swift app, CLI, Info.plist, signed.

    python3 apps/track-catcher-mac/scripts/bundle.py --sign "Developer ID Application: ..."
    python3 apps/track-catcher-mac/scripts/bundle.py --adhoc     # CI only

Why one identity: macOS keys the microphone grant to the code signature. An
ad-hoc signature changes with every build, so each rebuild is a new app to
TCC and asks for the mic again. Sign every build, dev builds too, with the
same identity (Developer ID, or one fixed local code-signing certificate) and
the grant is asked once. `--adhoc` exists for CI, where no mic is used, and
says so loudly.

The identity can also come from TC_SIGN_IDENTITY. Output: apps/track-catcher-mac/dist/.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REPO = HERE.parent.parent
ENGINE = REPO / "apps" / "live-core"
APP_NAME = "Open DJ Track Catcher.app"


def tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        sys.exit(f"bundle: {name} not found on PATH")
    return path


def run(*args: str, cwd: Path | None = None) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, check=True)


def build_engine() -> Path:
    """Build odj-live and return the executable Cargo says it wrote.

    CARGO_TARGET_DIR, build.target-dir and build.target all move the output,
    so the path comes from Cargo's own compiler-artifact message.
    """
    cmd = [
        tool("cargo"),
        "build",
        "--release",
        "--locked",
        "--bin",
        "odj-live",
        "--message-format=json-render-diagnostics",
    ]
    print("+", " ".join(cmd), flush=True)
    out = subprocess.run(cmd, cwd=ENGINE, check=True, stdout=subprocess.PIPE, text=True).stdout
    exe = None
    for line in out.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("reason") == "compiler-artifact" and msg.get("target", {}).get("name") == "odj-live":
            exe = msg.get("executable") or exe
    if not exe:
        sys.exit("bundle: cargo reported no odj-live executable")
    return Path(exe)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sign", default=os.environ.get("TC_SIGN_IDENTITY"), help="codesign identity")
    ap.add_argument("--adhoc", action="store_true", help="ad-hoc sign (CI only: the mic grant will not persist)")
    ap.add_argument("--out", type=Path, default=HERE / "dist")
    args = ap.parse_args()
    if sys.platform != "darwin":
        sys.exit("bundle: builds a macOS .app, so it runs on macOS only")
    if not args.sign and not args.adhoc:
        sys.exit(
            "bundle: no signing identity. Pass --sign IDENTITY (or set TC_SIGN_IDENTITY) so the mic grant "
            "is remembered across builds; --adhoc is for CI only."
        )
    if args.sign == "-" and not args.adhoc:
        sys.exit("bundle: '-' is the ad-hoc identity; pass --adhoc to ask for that (CI only).")
    identity = args.sign or "-"
    if identity == "-":
        print("bundle: AD-HOC signature. macOS will ask for the mic again after every rebuild.", file=sys.stderr)

    engine_exe = build_engine()
    run(tool("swift"), "build", "-c", "release", "--product", "TrackCatcher", cwd=HERE)
    run(tool("swift"), "build", "-c", "release", "--product", "track-catcher", cwd=HERE)
    bin_dir = Path(
        subprocess.run(
            [tool("swift"), "build", "-c", "release", "--show-bin-path"],
            cwd=HERE,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )

    app = args.out / APP_NAME
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    shutil.copy2(HERE / "Resources" / "Info.plist", app / "Contents" / "Info.plist")
    # IBM Plex (OFL-1.1, license beside the faces), found through ATSApplicationFontsPath.
    shutil.copytree(HERE / "Resources" / "Fonts", app / "Contents" / "Resources" / "Fonts")
    shutil.copy2(bin_dir / "TrackCatcher", macos / "TrackCatcher")
    shutil.copy2(bin_dir / "track-catcher", macos / "track-catcher")
    shutil.copy2(engine_exe, macos / "odj-live")

    codesign = tool("codesign")
    entitlements = HERE / "Resources" / "TrackCatcher.entitlements"
    # Inner executables first, then the app with its entitlements; no --deep.
    for inner in ("odj-live", "track-catcher"):
        run(codesign, "--force", "--options", "runtime", "--timestamp=none", "--sign", identity, str(macos / inner))
    run(
        codesign,
        "--force",
        "--options",
        "runtime",
        "--timestamp=none",
        "--entitlements",
        str(entitlements),
        "--sign",
        identity,
        str(app),
    )
    run(codesign, "--verify", "--strict", "--verbose=2", str(app))
    print(f"bundle: built {app}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
