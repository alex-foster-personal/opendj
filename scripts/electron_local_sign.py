"""Developer ID sign a packed Electron app for this Mac only (``--local-fast``).

``scripts/ship_electron.py --local-fast`` builds the Electron app on this Mac
and installs it on this Mac only, signed with this Mac's Developer ID
Application identity and the hardened runtime, and NOT notarized. It mirrors
the Tauri ``ship_dmg.sh --local-fast`` path (#8225). The point is a stable
code identity: macOS privacy (TCC) keys a signed app's grants (microphone,
input monitoring, automation) to its designated requirement, bundle id plus
Team ID, so a rebuild signed with the same identity keeps them, where an
unsigned or ad-hoc build is a new app to TCC on every rebuild.

What this module owns, and only this:

- ``resolve_signing_identity``: the identity, from ``MDT_MACOS_SIGNING_IDENTITY``,
  else the one ``~/.zshenv`` exports, else the keychain search list's single
  valid "Developer ID Application" identity. More than one, or none, refuses.
- ``nested_code_order``: every piece of nested code in the packed app, inside
  out (loose Mach-O files, then frameworks, then helper apps, then the app),
  the order codesign needs so no signature is sealed over an unsigned child.
  The engine payload is excluded: ``scripts/sign_macos_developer_id.sh
  payload`` signs it with the engine entitlements before it is copied in.
- ``sign_app`` / ``verify_signed_app``: run codesign over that order and prove
  the result (strict deep verify, hardened runtime flag, Developer ID
  authority, the app entitlements present, and the microphone prompt text in
  ``Contents/Info.plist``).

Not done here: notarization, stapling, a dmg, Gatekeeper assessment. Local-fast
skips them (``spctl`` rejects an un-notarized app; it is never quarantined
because it never left this Mac). ``scripts/electron_release.py`` signs with
``timestamp=True`` and then notarizes, staples and assesses.

Acceptance:

- [if] no identity is configured and the keychain search list holds zero or
  several Developer ID Application identities [then] resolution raises naming
  the three sources [else stop].
- [if] the packed app holds a Mach-O inside a framework or helper [then] it is
  signed before the bundle that contains it, and the app last [else stop].
- [if] a file under ``Contents/Resources/payload`` is Mach-O [then] this
  module never re-signs it (the payload carries engine entitlements) [else stop].
- [if] the signed app lacks the hardened runtime, the Developer ID authority,
  or an entitlement in ``ELECTRON_ENTITLEMENTS`` [then] verification raises
  [else stop].
- [if] the signed app's ``Info.plist`` has no non-empty
  ``NSMicrophoneUsageDescription`` [then] verification raises: macOS would never
  ask for the microphone, and the audio-input entitlement alone delivers
  silence (parity matrix row 45, ELECTRON-8399) [else stop].

-Claude
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ELECTRON_ENTITLEMENTS_REL = "apps/desktop/electron/entitlements.mac.plist"
PAYLOAD_SIGN_SCRIPT_REL = "scripts/sign_macos_developer_id.sh"
IDENTITY_ENV = "MDT_MACOS_SIGNING_IDENTITY"
DEVELOPER_ID_PREFIX = "Developer ID Application: "
# Keys the app and its helpers must carry. allow-jit: V8 in the main process
# and renderers. allow-unsigned-executable-memory: Chromium's helpers on
# Electron 44 still map writable-executable pages outside MAP_JIT.
# audio-input: the headphone cue calibration (CUEOUT-11) listens in the page,
# and a hardened app without it is refused every input silently.
ELECTRON_ENTITLEMENTS = (
    "com.apple.security.cs.allow-jit",
    "com.apple.security.cs.allow-unsigned-executable-memory",
    "com.apple.security.device.audio-input",
)
# The microphone prompt text. A hardened app that holds audio-input but no
# usage string is never shown the prompt and records silence (row 45). It comes
# from package.json build.mac.extendInfo, so a signed build proves it landed.
MICROPHONE_USAGE_KEY = "NSMicrophoneUsageDescription"
# codesign -dv prints this for an app signed with the hardened runtime.
RUNTIME_FLAG = re.compile(r"flags=0x[0-9a-f]*\([^)]*\bruntime\b[^)]*\)")
PAYLOAD_PARTS = ("Contents", "Resources", "payload")
MACHO_MAGICS = {bytes.fromhex(m) for m in ("feedface", "feedfacf", "cefaedfe", "cffaedfe", "cafebabe", "bebafeca")}

Run = Callable[[Sequence[str]], subprocess.CompletedProcess]


class LocalSignError(RuntimeError):
    """Signing or its proof failed. The message names the file and what codesign said."""


@dataclass(frozen=True)
class SignTarget:
    path: Path
    entitlements: bool


# ----- identity -------------------------------------------------------------


def resolve_tool(name: str) -> str:
    """Absolute path of ``name`` on PATH (AGENTS.md "Launch programs by resolved path")."""
    found = shutil.which(name)
    if found is None:
        raise LocalSignError(f"{name} is not on PATH; local-fast signing needs the Xcode command line tools")
    return os.path.abspath(found)


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run([resolve_tool(argv[0]), *argv[1:]], capture_output=True, text=True, check=False)


def developer_id_identities(find_identity_output: str) -> list[str]:
    """The quoted "Developer ID Application: ..." names in ``security find-identity -v`` output."""
    names = re.findall(r'"(Developer ID Application: [^"]+)"', find_identity_output)
    return list(dict.fromkeys(names))


def resolve_signing_identity(env: Mapping[str, str], run: Run = _run) -> str:
    """Env, then ~/.zshenv, then the search list's single Developer ID identity (same order as #8225)."""
    configured = env.get(IDENTITY_ENV, "").strip()
    if configured:
        return configured
    zshenv = Path(env.get("HOME", str(Path.home()))) / ".zshenv"
    zsh = shutil.which("zsh")
    if zsh is not None and zshenv.is_file():
        out = run([zsh, "-c", f'source {str(zshenv)!r} 2>/dev/null; print -r -- "${{{IDENTITY_ENV}:-}}"'])
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    listing = run(["security", "find-identity", "-v", "-p", "codesigning"])
    if listing.returncode != 0:
        raise LocalSignError(f"security find-identity failed: {(listing.stderr or listing.stdout).strip()}")
    found = developer_id_identities(listing.stdout)
    if len(found) == 1:
        return found[0]
    raise LocalSignError(
        f"no single Developer ID identity: {IDENTITY_ENV} is unset, ~/.zshenv does not export it, and the "
        f"keychain search list holds {len(found)} valid Developer ID Application identities "
        f"({', '.join(found) or 'none'}). Set {IDENTITY_ENV} to the one to use."
    )


# ----- nested code ------------------------------------------------------------


def is_macho(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(4) in MACHO_MAGICS


def _is_bundle_main_executable(path: Path) -> bool:
    """``X.app/Contents/MacOS/*`` and ``Y.framework/Versions/V/Y``: signed with their bundle."""
    parts = path.parts
    if len(parts) >= 3 and parts[-2] == "MacOS" and parts[-3] == "Contents":
        return True
    return len(parts) >= 4 and parts[-3] == "Versions" and parts[-4] == f"{path.name}.framework"


def _in_payload(path: Path, app: Path) -> bool:
    return path.relative_to(app).parts[: len(PAYLOAD_PARTS)] == PAYLOAD_PARTS


def nested_code_order(app: Path) -> list[SignTarget]:
    """Inside out: loose Mach-O (deepest first), frameworks, helper apps, then the app itself."""
    contents = app / "Contents"
    loose: list[Path] = []
    for root, dirs, files in os.walk(contents):
        dirs.sort()
        for name in sorted(files):
            path = Path(root) / name
            if path.is_symlink() or _in_payload(path, app) or _is_bundle_main_executable(path):
                continue
            if is_macho(path):
                loose.append(path)
    loose.sort(key=lambda p: (-len(p.parts), str(p)))
    frameworks_dir = contents / "Frameworks"
    bundles = sorted(frameworks_dir.iterdir()) if frameworks_dir.is_dir() else []
    frameworks = [b for b in bundles if b.suffix == ".framework" and not b.is_symlink()]
    helpers = [b for b in bundles if b.suffix == ".app" and not b.is_symlink()]
    return [
        *(SignTarget(p, entitlements=False) for p in loose),
        *(SignTarget(p, entitlements=False) for p in frameworks),
        *(SignTarget(p, entitlements=True) for p in helpers),
        SignTarget(app, entitlements=True),
    ]


# ----- sign and prove ------------------------------------------------------------


def codesign_command(target: SignTarget, identity: str, entitlements: Path, timestamp: bool = False) -> list[str]:
    """Local-fast passes no secure timestamp (offline-capable rebuilds); a release needs one to notarize."""
    stamp = "--timestamp" if timestamp else "--timestamp=none"
    argv = ["codesign", "--force", "--options", "runtime", stamp, "--sign", identity]
    if target.entitlements:
        argv += ["--entitlements", str(entitlements)]
    return [*argv, str(target.path)]


def check_entitlements_file(entitlements: Path) -> None:
    declared = plistlib.loads(entitlements.read_bytes())
    missing = [key for key in ELECTRON_ENTITLEMENTS if declared.get(key) is not True]
    if missing:
        raise LocalSignError(f"{entitlements} does not grant {', '.join(missing)}")


def sign_app(app: Path, identity: str, entitlements: Path, run: Run = _run, timestamp: bool = False) -> int:
    """Sign every nested piece inside out, then the app. Returns how many codesign calls ran."""
    check_entitlements_file(entitlements)
    order = nested_code_order(app)
    for target in order:
        out = run(codesign_command(target, identity, entitlements, timestamp))
        if out.returncode != 0:
            raise LocalSignError(f"codesign failed on {target.path}: {(out.stderr or out.stdout).strip()}")
    return len(order)


def verify_signed_app(app: Path, run: Run = _run, require_timestamp: bool = False) -> list[str]:
    """Strict deep verify, then the hardened runtime, the Developer ID authority and the entitlements.

    ``require_timestamp``: a release (notarized) signature must carry a secure timestamp.
    """
    verify = run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
    if verify.returncode != 0:
        raise LocalSignError(f"codesign --verify --deep --strict failed on {app}: {verify.stderr.strip()}")
    details = run(["codesign", "-dv", "--verbose=4", str(app)])
    described = details.stderr + details.stdout
    if details.returncode != 0:
        raise LocalSignError(f"codesign -dv failed on {app}: {described.strip()}")
    if RUNTIME_FLAG.search(described) is None:
        raise LocalSignError(f"{app} is signed without the hardened runtime")
    authority = re.search(rf"^Authority=({re.escape(DEVELOPER_ID_PREFIX)}.+)$", described, re.MULTILINE)
    if authority is None:
        raise LocalSignError(f"{app} is not signed by a Developer ID Application identity")
    if require_timestamp and re.search(r"^Timestamp=", described, re.MULTILINE) is None:
        raise LocalSignError(f"{app} carries no secure timestamp; the notary service rejects that")
    granted = run(["codesign", "-d", "--entitlements", "-", "--xml", str(app)])
    missing = [key for key in ELECTRON_ENTITLEMENTS if key not in granted.stdout]
    if granted.returncode != 0 or missing:
        raise LocalSignError(f"{app} lacks the entitlements {', '.join(missing) or '(unreadable)'}")
    microphone_usage(app)
    return [
        f"[OK] {app.name}: codesign --verify --deep --strict passed",
        f"[OK] {app.name}: hardened runtime, {authority.group(1)}",
        f"[OK] {app.name}: entitlements {', '.join(ELECTRON_ENTITLEMENTS)}",
        f"[OK] {app.name}: Info.plist {MICROPHONE_USAGE_KEY} present",
    ]


def microphone_usage(app: Path) -> str:
    """The app's ``NSMicrophoneUsageDescription``; raises when the plist is unreadable or the text is empty."""
    info = app / "Contents" / "Info.plist"
    try:
        text = plistlib.loads(info.read_bytes()).get(MICROPHONE_USAGE_KEY)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise LocalSignError(f"{info} is unreadable: {exc}") from exc
    if not isinstance(text, str) or not text.strip():
        raise LocalSignError(
            f"{info} has no {MICROPHONE_USAGE_KEY}: macOS never asks for the microphone and audio input is silent"
        )
    return text
