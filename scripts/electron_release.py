"""Build a signed, notarized, stapled Electron release into a local directory.

Spec: ``(private path)`` (parity matrix rows 59, 60,
62). This builds and verifies; it NEVER publishes. Publishing to the public
updater host is a separate step, ``scripts/electron_publish.py`` (spec
"Delivery plan" step 5), run on a finished ``--out``.

What a run produces in ``--out`` (no artifact name carries a space, because
GitHub rewrites spaces in asset names and a feed URL would 404):

- ``OpenDJ-<v>-arm64.dmg``: signed, notarized, stapled disk image of the app,
  with an ``Applications`` link beside it so the DJ drags the app onto it
  (:func:`dmg_layout`, :func:`stage_dmg_root`, ELECTRON-8819).
- ``OpenDJ-<v>-arm64-mac.zip``: the stapled app, the Squirrel.Mac payload.
- ``latest-mac.yml``: the electron-updater generic feed naming that zip by
  sha512 and size, with ``releaseNotes: built from <sha>``.
- ``OpenDJ-<v>-aarch64.app.tar.gz`` + ``.sig``: the same stapled app packed for
  ``tauri-plugin-updater`` and minisign-signed with the Tauri updater key, so
  an alpha Tauri install updates INTO the Electron app (the bridge).
- ``latest.json``: the Tauri feed naming that archive.
- ``release-evidence.json``: version, sha, notarization ids, spctl output,
  sha512 and size of every artifact.

Signing reuses ``scripts/electron_local_sign.py`` (inside out, payload
excluded, now with a secure timestamp) and ``scripts/sign_macos_developer_id.sh``
(payload, notarize-app, dmg, notarize). electron-builder only packs; its own
signer would re-sign the payload with the shell entitlements.

Versions: a release's version must equal ``package.json`` ``version`` (the
one app version source, ``scripts/app_identity.py``: the payload's
``app_version`` and the engine's ``/build-info`` read it) and, while the Tauri
shell still ships, its copy in ``tauri.conf.json``, so a Tauri install bridged
to this release and the Electron updater agree. ``--proof`` lifts that
for a private update-install test: the shell gets the version through
``-c.extraMetadata.version`` and the staged payload manifest's ``app_version``
is rewritten to match, and the evidence says ``proof: true``.

``--proof-env KEY=VALUE`` (proof builds only, repeatable) bakes
``LSEnvironment`` into the proof bundle's ``Info.plist``. Squirrel.Mac's ShipIt
relaunches an updated app through LaunchServices with no environment of its
own, so this is the only way the relaunched N+1 keeps the test's isolated
home, data dir and private feed instead of opening the machine's real
library. A release build refuses it.

Acceptance:

- [if] MDT_SHIP_UNSIGNED is set, or no notary profile is configured [then]
  the run refuses before building [else stop].
- [if] a non-proof version differs from package.json or tauri.conf.json [then]
  the run refuses naming all three [else stop].
- [if] latest-mac.yml names a sha512 or size that is not the zip on disk
  [then] verification raises [else stop].
- [if] spctl does not say accepted with source=Notarized Developer ID [then]
  verification raises carrying spctl's output [else stop].
- [if] the bridge signature file is empty [then] building latest.json raises
  [else stop].
- [if] --proof-env is given without --proof, or not as KEY=VALUE [then] the
  run refuses before building [else stop].
- [if] a non-proof release has no --notes [then] the run refuses before
  building [else stop].
- [if] the dmg volume root has no ``Applications`` symlink to
  ``/Applications`` beside the app [then] staging raises and no dmg is built
  [else stop].

Usage::

    uv run --no-sync python -m scripts.electron_release --out <dir> --notes "<line>" [--version V --proof] [--channel nightly]

Publishing is ``scripts/electron_publish.py``, run on the finished ``--out``.

-Claude
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from apps.shared.semver import parse_semver
from scripts import electron_local_sign, ship_electron
from scripts.release_identity import release_asset_url

REPO_ROOT = Path(__file__).resolve().parents[1]
ELECTRON_DIR = REPO_ROOT / "apps/desktop/electron"
PACKAGE_JSON = ELECTRON_DIR / "package.json"
TAURI_CONF = REPO_ROOT / "apps/desktop/src-tauri/tauri.conf.json"
PUBLIC_REPO = "alex-foster-personal/issue-assets"
SIGN_SCRIPT = "scripts/sign_macos_developer_id.sh"
ARCHIVE_SCRIPT = "scripts/pack_updater_archive.sh"
PLATFORM_KEY = "darwin-aarch64"
CHANNELS = ("nightly", "stable")
STAMP_LANE = "electron"
NOTARY_PROFILE_ENV = "MDT_MACOS_NOTARY_KEYCHAIN_PROFILE"
TAURI_KEY_ENV = "TAURI_SIGNING_PRIVATE_KEY"
UNSIGNED_ENV = "MDT_SHIP_UNSIGNED"
NOTARY_ID = re.compile(r"^\s*id: ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s*$", re.MULTILINE)
NOTARIZED_SOURCE = "source=Notarized Developer ID"
#: The feed directory the shell installs from; a test pins it to UPDATE_FEED_URL in src/updater.ts.
UPDATE_FEED_URL = "https://github.com/alex-foster-personal/issue-assets/releases/latest/download"
#: electron-updater's download cache folder name, electron-builder's `${name}-updater`.
UPDATER_CACHE_DIR = "opendj-desktop-electron-updater"


class ReleaseError(RuntimeError):
    """A release step failed or its proof did not hold. The message names what and why."""


@dataclass(frozen=True)
class Artifacts:
    out: Path
    version: str

    @property
    def dmg(self) -> Path:
        return self.out / f"OpenDJ-{self.version}-arm64.dmg"

    @property
    def zip(self) -> Path:
        return self.out / f"OpenDJ-{self.version}-arm64-mac.zip"

    @property
    def feed(self) -> Path:
        return self.out / "latest-mac.yml"

    @property
    def archive(self) -> Path:
        return self.out / f"OpenDJ-{self.version}-aarch64.app.tar.gz"

    @property
    def signature(self) -> Path:
        return self.out / f"OpenDJ-{self.version}-aarch64.app.tar.gz.sig"

    @property
    def bridge(self) -> Path:
        return self.out / "latest.json"

    @property
    def evidence(self) -> Path:
        return self.out / "release-evidence.json"

    @property
    def app(self) -> Path:
        return self.out / "app" / ship_electron.APP_NAME


# ----- dmg layout --------------------------------------------------------------

#: The drag-to-install target every Mac dmg carries: a symlink named Applications.
APPLICATIONS_LINK_NAME = "Applications"
APPLICATIONS_LINK_TARGET = "/Applications"


def dmg_layout(app_name: str) -> dict[str, str | None]:
    """The dmg volume root: entry name -> symlink target, None for the app bundle itself."""
    return {app_name: None, APPLICATIONS_LINK_NAME: APPLICATIONS_LINK_TARGET}


def stage_dmg_root(root: Path, app_name: str) -> None:
    """Lay ``root`` out as :func:`dmg_layout` says: the app must be there, each link is made."""
    for name, target in dmg_layout(app_name).items():
        entry = root / name
        if target is None:
            if not entry.is_dir():
                raise ReleaseError(f"dmg root {root} has no {name}; package() runs after the app is notarized")
        elif not entry.is_symlink():
            entry.symlink_to(target)
        elif os.readlink(entry) != target:
            raise ReleaseError(f"{entry} links to {os.readlink(entry)}, not {target}")
    extra = sorted(p.name for p in root.iterdir() if p.name not in dmg_layout(app_name))
    if extra:
        raise ReleaseError(f"dmg root {root} carries entries the layout does not name: {', '.join(extra)}")


# ----- versions ----------------------------------------------------------------


def resolve_release_version(requested: str | None, package_version: str, tauri_version: str, proof: bool) -> str:
    """The release semver. A real release has one version source; a proof may carry any valid semver."""
    version = requested or package_version
    if version != version.strip() or version.startswith("v"):
        raise ReleaseError(f"release version {version!r} must be bare semver (no whitespace, no leading v)")
    parse_semver(version)
    if proof:
        return version
    if not version == package_version == tauri_version:
        raise ReleaseError(
            f"release version {version} must equal apps/desktop/electron/package.json ({package_version}), the app "
            f"version source, and its Tauri copy tauri.conf.json ({tauri_version}); bump them together, or pass "
            "--proof for a private update-install build"
        )
    return version


def proof_environment_args(pairs: Sequence[str], proof: bool) -> list[str]:
    """electron-builder ``-c`` flags that bake LSEnvironment into a proof bundle's Info.plist."""
    if pairs and not proof:
        raise ReleaseError("--proof-env bakes a test environment into the bundle; it is refused without --proof")
    flags: list[str] = []
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise ReleaseError(f"--proof-env {pair!r} is not KEY=VALUE with an upper-case environment name")
        flags.append(f"-c.mac.extendInfo.LSEnvironment.{key}={value}")
    return flags


def app_update_config() -> str:
    """``Contents/Resources/app-update.yml``: electron-updater's downloadUpdate() opens it even after setFeedURL()."""
    return yaml.safe_dump(
        {"provider": "generic", "url": UPDATE_FEED_URL, "updaterCacheDirName": UPDATER_CACHE_DIR}, sort_keys=False
    )


def write_app_update_config(app: Path) -> None:
    """electron-builder writes it only for updatable targets, never for ``--dir``; measured on the Air,
    Tue 3 Nov 2026: a ``--dir`` pack had none and downloadUpdate() failed ENOENT. Written before signing."""
    (app / "Contents" / "Resources" / "app-update.yml").write_text(app_update_config(), encoding="utf-8")


def set_payload_app_version(stage: Path, version: str) -> None:
    """Proof builds only: make the staged payload manifest's app_version the proof version."""
    manifest_path = stage / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    identity = manifest.get("identity")
    if not isinstance(identity, dict) or "app_version" not in identity:
        raise ReleaseError(f"{manifest_path} has no identity.app_version to set")
    identity["app_version"] = version
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


# ----- feeds -------------------------------------------------------------------


def sha512_b64(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def release_notes(notes: str, git_sha: str) -> str:
    """Feed notes: the user-facing line, then ``built from <sha>``, which the engine's
    same-version check and release.sh's retry validation read."""
    build_line = f"built from {git_sha}"
    return f"{notes.strip()}\n\n{build_line}" if notes.strip() else build_line


def latest_mac_yml(version: str, zip_path: Path, git_sha: str, release_date: datetime, notes: str = "") -> str:
    """electron-updater generic feed. The url is relative: it resolves against the feed directory."""
    digest = sha512_b64(zip_path)
    feed = {
        "version": version,
        "files": [{"url": zip_path.name, "sha512": digest, "size": zip_path.stat().st_size}],
        "path": zip_path.name,
        "sha512": digest,
        "releaseDate": release_date.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "releaseNotes": release_notes(notes, git_sha),
    }
    return yaml.safe_dump(feed, sort_keys=False)


def check_feed_matches(feed_text: str, zip_path: Path, version: str) -> None:
    """Raise unless the feed names exactly these zip bytes at this version."""
    feed = yaml.safe_load(feed_text)
    files = feed.get("files") or []
    if feed.get("version") != version:
        raise ReleaseError(f"latest-mac.yml version is {feed.get('version')!r}, expected {version}")
    if len(files) != 1 or files[0].get("url") != zip_path.name or feed.get("path") != zip_path.name:
        raise ReleaseError(f"latest-mac.yml does not name {zip_path.name} as its only file: {files}")
    actual = sha512_b64(zip_path)
    if files[0].get("sha512") != actual or feed.get("sha512") != actual:
        raise ReleaseError(f"latest-mac.yml sha512 does not match {zip_path.name}")
    if files[0].get("size") != zip_path.stat().st_size:
        raise ReleaseError(f"latest-mac.yml size {files[0].get('size')} is not {zip_path.stat().st_size}")


def bridge_manifest(
    version: str, git_sha: str, archive_name: str, signature: str, channel: str, pub_date: datetime, notes: str = ""
) -> dict:
    """The Tauri ``latest.json`` whose archive is the Electron app (spec section c)."""
    if not signature.strip():
        raise ReleaseError("the bridge archive's minisign signature is empty")
    return {
        "version": version,
        "notes": release_notes(notes, git_sha),
        "pub_date": pub_date.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "channel": channel,
        "platforms": {
            PLATFORM_KEY: {"signature": signature.strip(), "url": release_asset_url(PUBLIC_REPO, version, archive_name)}
        },
    }


# ----- verification parsing ------------------------------------------------------


def require_spctl_accepted(output: str, subject: Path) -> None:
    if f"{subject}: accepted" not in output or NOTARIZED_SOURCE not in output:
        raise ReleaseError(f"Gatekeeper did not accept {subject} as notarized:\n{output.strip()}")


def notarization_ids(output: str) -> list[str]:
    return list(dict.fromkeys(NOTARY_ID.findall(output)))


# ----- running ------------------------------------------------------------------


def run(argv: Sequence[str], env: Mapping[str, str] | None = None, cwd: Path | None = None) -> str:
    """Run a resolved command, echo it, stream nothing, return stdout+stderr; raise on non-zero."""
    print(f"[RUN] {' '.join(ship_electron._q(a) for a in argv)}", flush=True)
    done = subprocess.run(
        ship_electron._resolved(argv),
        env=dict(env) if env is not None else None,
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    output = done.stdout + done.stderr
    if done.returncode != 0:
        raise ReleaseError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} exited {done.returncode}:\n{output[-4000:]}")
    return output


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout.strip()


@dataclass
class Options:
    out: Path
    version: str
    proof: bool
    channel: str
    proof_env: tuple[str, ...] = ()
    notes: str = ""


class Release:
    """One method per step, in the order ``main`` calls them."""

    def __init__(self, opts: Options, env: Mapping[str, str], git_sha: str) -> None:
        self.opts = opts
        self.env = dict(env)
        self.git_sha = git_sha
        self.art = Artifacts(opts.out, opts.version)
        self.stage = REPO_ROOT / ship_electron.PAYLOAD_STAGE_REL
        self.packed = REPO_ROOT / ship_electron.PACKED_APP_REL
        self.identity = ""
        self.evidence: dict = {
            "version": opts.version,
            "git_sha": git_sha,
            "proof": opts.proof,
            "channel": opts.channel,
            "proof_env": list(opts.proof_env),
            "notes": opts.notes,
        }

    def preflight(self) -> None:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise ReleaseError(f"a release needs an arm64 Mac; this is {platform.system()}/{platform.machine()}")
        if self.env.get(UNSIGNED_ENV):
            raise ReleaseError(f"{UNSIGNED_ENV} must not be set for a release")
        for name in (NOTARY_PROFILE_ENV, TAURI_KEY_ENV):
            if not self.env.get(name):
                raise ReleaseError(f"{name} is required for a release")
        dirty = _git("status", "--porcelain")
        if dirty:
            raise ReleaseError(f"the checkout is dirty; the stamps would say git_dirty=1:\n{dirty}")
        self.identity = electron_local_sign.resolve_signing_identity(self.env)
        self.env[electron_local_sign.IDENTITY_ENV] = self.identity
        if self.opts.out.exists() and any(self.opts.out.iterdir()):
            raise ReleaseError(f"{self.opts.out} is not empty; pick a fresh output directory")
        self.opts.out.mkdir(parents=True, exist_ok=True)

    def build_payload(self) -> None:
        if (REPO_ROOT / ship_electron.PACKED_APP_REL).parent.exists():
            shutil.rmtree((REPO_ROOT / ship_electron.PACKED_APP_REL).parent)
        frontend = REPO_ROOT / ship_electron.FRONTEND_DIR_REL
        run(["pnpm", "install", "--frozen-lockfile"], self.env, frontend)
        run(["pnpm", "run", "build"], self.env, frontend)
        run(
            [
                "uv",
                "run",
                "--no-sync",
                "python",
                "-m",
                "scripts.build_engine_payload",
                "--out",
                str(self.stage),
                "--label",
                "",
            ],
            self.env,
        )
        if self.opts.proof:
            set_payload_app_version(self.stage, self.opts.version)
        run(["bash", SIGN_SCRIPT, "payload", str(self.stage)], self.env)

    def pack(self) -> None:
        env = {
            **self.env,
            "OPENDJ_BUILD_LANE_LABEL": STAMP_LANE,
            "OPENDJ_BUILD_CHANNEL": self.opts.channel,
            "CSC_IDENTITY_AUTO_DISCOVERY": "false",
        }
        env.pop("MDT_LANE_LABEL", None)
        run(["pnpm", "install", "--frozen-lockfile"], env, ELECTRON_DIR)
        run(["pnpm", "exec", "tsc", "-p", "tsconfig.json"], env, ELECTRON_DIR)
        run(["node", "scripts/stamp-build.mjs"], env, ELECTRON_DIR)
        run(
            [
                "pnpm",
                "exec",
                "electron-builder",
                "--dir",
                "--mac",
                "--arm64",
                "--publish",
                "never",
                f"-c.extraMetadata.version={self.opts.version}",
                *proof_environment_args(self.opts.proof_env, self.opts.proof),
            ],
            env,
            ELECTRON_DIR,
        )
        write_app_update_config(self.packed)
        ship_electron.replace_placeholder_payload(self.packed, self.stage)
        for line in ship_electron.verify_packed_app(self.packed, self.git_sha):
            print(line, flush=True)

    def sign_and_notarize_app(self) -> None:
        payload = self.packed / "Contents" / "Resources" / "payload"
        run(
            [
                "uv",
                "run",
                "--locked",
                "--no-sync",
                "python",
                "-m",
                "scripts.payload_signed_manifest",
                "--payload",
                str(payload),
            ],
            self.env,
        )
        entitlements = REPO_ROOT / electron_local_sign.ELECTRON_ENTITLEMENTS_REL
        signed = electron_local_sign.sign_app(self.packed, self.identity, entitlements, timestamp=True)
        print(f"[OK] signed {signed} pieces of nested code inside out with secure timestamps", flush=True)
        for line in electron_local_sign.verify_signed_app(self.packed, require_timestamp=True):
            print(line, flush=True)
        self.art.app.parent.mkdir(parents=True)
        run(["ditto", str(self.packed), str(self.art.app)])
        out = run(["bash", SIGN_SCRIPT, "notarize-app", str(self.art.app)], self.env)
        self.evidence["notarization_app"] = notarization_ids(out)

    def package(self) -> None:
        app = self.art.app
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(self.art.zip)])
        # The app's own folder is the volume root, so the image carries the app
        # and the Applications link and nothing else (no second copy of the app).
        stage_dmg_root(app.parent, app.name)
        run(
            [
                "hdiutil",
                "create",
                "-volname",
                "Open DJ",
                "-srcfolder",
                str(app.parent),
                "-ov",
                "-format",
                "UDZO",
                str(self.art.dmg),
            ]
        )
        run(["bash", SIGN_SCRIPT, "dmg", str(self.art.dmg)], self.env)
        out = run(["bash", SIGN_SCRIPT, "notarize", str(self.art.dmg)], self.env)
        self.evidence["notarization_dmg"] = notarization_ids(out)
        run(["bash", ARCHIVE_SCRIPT, str(app), str(self.art.archive)])
        # The updater key has no passphrase (docs/auto-update.md "Keys"); an
        # unset password variable makes the signer prompt, which hangs a run.
        signer_env = {
            **self.env,
            "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": self.env.get("TAURI_SIGNING_PRIVATE_KEY_PASSWORD", ""),
        }
        run(
            ["cargo", "tauri", "signer", "sign", str(self.art.archive)],
            signer_env,
            REPO_ROOT / "apps/desktop/src-tauri",
        )

    def write_feeds(self) -> None:
        now = datetime.now(UTC)
        self.art.feed.write_text(
            latest_mac_yml(self.opts.version, self.art.zip, self.git_sha, now, self.opts.notes), encoding="utf-8"
        )
        if not self.art.signature.is_file():
            raise ReleaseError(f"cargo tauri signer sign wrote no {self.art.signature.name}")
        signature = self.art.signature.read_text(encoding="utf-8")
        manifest = bridge_manifest(
            self.opts.version, self.git_sha, self.art.archive.name, signature, self.opts.channel, now, self.opts.notes
        )
        self.art.bridge.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    def verify(self) -> None:
        app, dmg = self.art.app, self.art.dmg
        run(["xcrun", "stapler", "validate", str(app)])
        run(["xcrun", "stapler", "validate", str(dmg)])
        app_spctl = run(["spctl", "-a", "-t", "exec", "-vv", str(app)])
        require_spctl_accepted(app_spctl, app)
        dmg_spctl = run(["spctl", "-a", "-t", "open", "--context", "context:primary-signature", "-vv", str(dmg)])
        require_spctl_accepted(dmg_spctl, dmg)
        run(["codesign", "--verify", "--deep", "--strict", str(app)])
        check_feed_matches(self.art.feed.read_text(encoding="utf-8"), self.art.zip, self.opts.version)
        with self.art.archive.open("rb") as handle:
            if handle.read(2) != b"\x1f\x8b":
                raise ReleaseError(f"{self.art.archive.name} is not gzip; the Tauri updater would refuse it")
        self.evidence["spctl_app"] = app_spctl.strip()
        self.evidence["spctl_dmg"] = dmg_spctl.strip()

    def write_evidence(self) -> None:
        files = [self.art.dmg, self.art.zip, self.art.archive, self.art.signature, self.art.feed, self.art.bridge]
        self.evidence["artifacts"] = {p.name: {"sha512": sha512_b64(p), "size": p.stat().st_size} for p in files}
        self.evidence["written_at_utc"] = datetime.now(UTC).replace(microsecond=0).isoformat()
        self.art.evidence.write_text(json.dumps(self.evidence, indent=2) + "\n", encoding="utf-8")


# ----- CLI ----------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="empty or new directory for the artifacts")
    parser.add_argument("--version", default=None, help="release semver; defaults to package.json version")
    parser.add_argument(
        "--proof", action="store_true", help="private update-install build: any semver, evidence says proof"
    )
    parser.add_argument("--channel", choices=CHANNELS, default="nightly")
    parser.add_argument(
        "--proof-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="proof builds only: bake LSEnvironment KEY=VALUE into Info.plist (repeatable)",
    )
    parser.add_argument(
        "--notes",
        default="",
        help="user-facing release notes line for both feeds; required for a public (non-proof) release",
    )
    return parser.parse_args(argv)


def require_release_notes(notes: str, proof: bool) -> None:
    """A public release always tells users what changed; a proof build may stay silent."""
    if not proof and not notes.strip():
        raise ReleaseError("a public (non-proof) release needs --notes, the line users see in the update prompt")


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    environ = dict(os.environ if env is None else env)
    try:
        package_version = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["version"]
        tauri_version = json.loads(TAURI_CONF.read_text(encoding="utf-8"))["version"]
        version = resolve_release_version(args.version, package_version, tauri_version, args.proof)
        proof_environment_args(args.proof_env, args.proof)
        require_release_notes(args.notes, args.proof)
        opts = Options(
            out=args.out.resolve(),
            version=version,
            proof=args.proof,
            channel=args.channel,
            proof_env=tuple(args.proof_env),
            notes=args.notes,
        )
        release = Release(opts, environ, _git("rev-parse", "HEAD"))
        for step in (
            release.preflight,
            release.build_payload,
            release.pack,
            release.sign_and_notarize_app,
            release.package,
            release.write_feeds,
            release.verify,
            release.write_evidence,
        ):
            print(f"[STEP] {step.__name__}", flush=True)
            step()
    except (ReleaseError, electron_local_sign.LocalSignError, ship_electron.ShipError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] release {version} built and verified in {opts.out} (not published)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
