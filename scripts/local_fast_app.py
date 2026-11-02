"""Seal and verify a LOCAL-FAST Open DJ app: Developer ID signed, never notarized.

``ship_dmg.sh --local-fast`` (this machine builds, this machine installs) runs
``MDT_LOCAL_FAST_UNNOTARIZED=1 just dmg``, which stops after the outer
re-seal: no app notarization, no dmg, no dmg notarization. That saves about
14 of a 23 minute build (demon-llama, Mon 2 Nov 2026). A locally built app
carries no com.apple.quarantine, so Gatekeeper never assesses it, and the
Developer ID signature keeps macOS privacy grants across rebuilds.

It must never pass for a shippable build, so:

* the payload manifest's ``release_channel`` is ``local-unnotarized``
  (stamped by the recipe before the payload is signed, so it is sealed in and
  shown in the app's build identity);
* ``seal`` writes ``<App>.app.local-fast`` beside the built app: the commit,
  build time and the bundle's CDHash, with ``notarized: false``;
* ``verify`` checks an app (the built one, or the copy installed in
  /Applications) against that marker by CDHash, so the bytes installed are
  the bytes sealed;
* ``spctl -a`` rejects an un-notarized Developer ID app, so the next normal
  ``ship_dmg.sh`` rollout plans a reinstall of the notarized build.

Stdlib only, Python 3.9 compatible (ship_dmg.sh calls it with python3).

Acceptance ((private path)):
  [if] the app is ad-hoc signed (no Developer ID authority) [then] seal fails, [else stop]
  [if] the app lacks the hardened runtime flag [then] seal fails, [else stop]
  [if] the manifest's release_channel is not local-unnotarized [then] seal fails, [else stop]
  [if] the manifest's git_sha_full is not the expected sha [then] seal fails, [else stop]
  [if] a sealed app's CDHash changes [then] verify fails, [else stop]
  [if] the marker is missing [then] verify fails, [else stop]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

LOCAL_FAST_CHANNEL = "local-unnotarized"
MARKER_SUFFIX = ".local-fast"
DEVELOPER_ID_PREFIX = "Developer ID Application: "
RUNTIME_FLAG = "(runtime)"
MANIFEST_REL = "Contents/Resources/payload/manifest.json"


class LocalFastError(RuntimeError):
    """The app is not a sealed local-fast build. The message says why."""


# ----- helpers -------------------------------------------------------------


def marker_path(app: Path) -> Path:
    return app.parent / f"{app.name}{MARKER_SUFFIX}"


def _manifest_identity(app: Path) -> dict[str, Any]:
    manifest = app / MANIFEST_REL
    try:
        body = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LocalFastError(f"no readable payload manifest at {manifest}: {exc}") from exc
    identity = body.get("identity")
    if not isinstance(identity, dict):
        raise LocalFastError(f"{manifest} carries no identity block")
    return identity


def _codesign_details(app: Path) -> dict[str, Any]:
    """Authorities, CDHash and hardened-runtime flag, as codesign reports them."""
    proc = subprocess.run(["codesign", "-dv", "--verbose=4", str(app)], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise LocalFastError(f"codesign -dv could not read {app}: {proc.stderr.strip()}")
    authorities: list[str] = []
    cdhash = ""
    runtime = False
    for line in proc.stderr.splitlines():
        if line.startswith("Authority="):
            authorities.append(line.split("=", 1)[1])
        elif line.startswith("CDHash="):
            cdhash = line.split("=", 1)[1].strip()
        elif line.startswith("CodeDirectory ") and RUNTIME_FLAG in line:
            runtime = True
    return {"authorities": authorities, "cdhash": cdhash, "runtime": runtime}


def _require_valid_developer_id(app: Path) -> str:
    """codesign --verify passes, Developer ID first authority, hardened runtime. Returns the CDHash."""
    check = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, text=True, check=False
    )
    if check.returncode != 0:
        raise LocalFastError(f"codesign --verify --deep --strict rejects {app}: {check.stderr.strip()}")
    details = _codesign_details(app)
    authorities = details["authorities"]
    if not authorities or not authorities[0].startswith(DEVELOPER_ID_PREFIX):
        raise LocalFastError(
            f"{app} is not Developer ID signed (authorities: {authorities or 'none, ad-hoc'}); "
            "a local-fast app must keep a Developer ID signature so macOS privacy grants survive rebuilds"
        )
    if not details["runtime"]:
        raise LocalFastError(f"{app} lacks the hardened runtime flag")
    if not details["cdhash"]:
        raise LocalFastError(f"codesign reported no CDHash for {app}")
    return str(details["cdhash"])


def _require_local_fast_identity(identity: dict[str, Any], expect_sha: str) -> None:
    channel = identity.get("release_channel")
    if channel != LOCAL_FAST_CHANNEL:
        raise LocalFastError(
            f"manifest release_channel is {channel!r}, not {LOCAL_FAST_CHANNEL!r}; "
            "only a build stamped as un-notarized may be installed as one"
        )
    sha = identity.get("git_sha_full")
    if sha != expect_sha:
        raise LocalFastError(f"manifest git_sha_full is {sha!r}, expected {expect_sha!r}")


# ----- seal / verify ---------------------------------------------------------


def seal(app: Path, expect_sha: str) -> Path:
    """Check the freshly built app and write its marker. Returns the marker path."""
    identity = _manifest_identity(app)
    _require_local_fast_identity(identity, expect_sha)
    cdhash = _require_valid_developer_id(app)
    marker = marker_path(app)
    body = {
        "git_sha_full": expect_sha,
        "built_at_utc": identity.get("built_at_utc"),
        "cdhash": cdhash,
        "release_channel": LOCAL_FAST_CHANNEL,
        "notarized": False,
    }
    marker.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return marker


def verify(app: Path, marker: Path) -> dict[str, Any]:
    """Check an app (built or installed) is the exact sealed local-fast build."""
    try:
        sealed = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LocalFastError(f"no readable local-fast marker at {marker}: {exc}") from exc
    if sealed.get("notarized") is not False or sealed.get("release_channel") != LOCAL_FAST_CHANNEL:
        raise LocalFastError(f"{marker} does not describe a local-fast build")
    _require_local_fast_identity(_manifest_identity(app), str(sealed.get("git_sha_full")))
    cdhash = _require_valid_developer_id(app)
    if cdhash != sealed.get("cdhash"):
        raise LocalFastError(
            f"{app} has CDHash {cdhash}, but {marker} sealed {sealed.get('cdhash')}; these are not the sealed bytes"
        )
    return sealed


# ----- CLI -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    seal_p = sub.add_parser("seal", help="check a freshly built app and write its marker")
    seal_p.add_argument("--app", required=True, type=Path)
    seal_p.add_argument("--expect-sha", required=True)
    verify_p = sub.add_parser("verify", help="check an app against a sealed marker")
    verify_p.add_argument("--app", required=True, type=Path)
    verify_p.add_argument("--marker", required=True, type=Path)
    opts = parser.parse_args(argv)
    try:
        if opts.cmd == "seal":
            print(f"[OK] local-fast marker: {seal(opts.app, opts.expect_sha)}")
        elif opts.cmd == "verify":
            sealed = verify(opts.app, opts.marker)
            print(f"[OK] {opts.app} is the sealed local-fast build {sealed['git_sha_full'][:8]} (NOT notarized)")
        else:
            raise AssertionError(f"unhandled command {opts.cmd}")
    except LocalFastError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
