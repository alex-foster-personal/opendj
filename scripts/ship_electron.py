"""Build the Electron desktop app and install it on the Macs (OPS-63).

Entry point: ``scripts/ship_dmg.sh --shell electron [...]`` (the ship-dmg
skill), which hands every other argument here. Stdlib only and Python 3.9
compatible, because a Mac coordinator may run it under ``/usr/bin/python3``.

What a live run does, in order (``--dry-run`` prints exactly these steps and
runs none of them):

1. Refuses a signed build. Signing and notarization of the Electron app need
   the owner's Apple credentials and are NOT implemented: without
   ``--unsigned`` (or ``MDT_SHIP_UNSIGNED=1``) the run exits 3 saying so,
   before it touches anything.
2. Checks it is on one of the three known arm64 Macs (any may build; silver
   since Mon 2 Nov 2026), in a clean
   checkout whose HEAD is the target commit.
3. Builds the SPA and the engine payload with the same commands the Tauri
   ``just dmg`` recipe uses (``scripts.build_engine_payload``, plain label),
   into ``apps/desktop/electron/out/payload-stage``.
4. Packs the shell: ``pnpm --dir apps/desktop/electron pack:dir`` (unsigned
   ``electron-builder --dir``, darwin arm64), stamped lane ``electron``,
   channel ``dev``.
5. Replaces the pack's placeholder ``Contents/Resources/payload`` with the
   staged payload, the way the #6117 bundle was assembled by hand.
6. Verifies the version stamps: the shell's ``dist/build-identity.json``
   inside ``app.asar``, the payload ``manifest.json`` and the target commit
   are one sha, and ``Info.plist`` carries the Open DJ bundle id and name.
7. On each selected Mac: refuses while the TESTMAC persona loop runs, quits a
   running ``Open DJ.app`` (AppleScript, polled; never a signal), whichever
   shell built it, deletes exactly that bundle and nothing else, copies the new one to
   ``/Applications``, clears quarantine (unsigned only), and reads the
   installed stamp back.

Not done here: relaunch and the engine health probe, ``opendj install-cli``,
signing, notarization, a dmg, and building on another machine.

Acceptance:

- [if] neither ``--unsigned`` nor ``MDT_SHIP_UNSIGNED=1`` is given [then] the
  run exits 3 naming the missing Apple credentials and runs nothing [else stop].
- [if] ``--dry-run --unsigned`` is given [then] the plan names every command a
  live run would run, installs only ``Open DJ.app``, and runs none
  of them [else stop].
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import shlex
import shutil
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

try:
    from scripts import electron_payload
except ImportError:  # run as a file: scripts/ship_electron.py
    import electron_payload  # type: ignore[no-redef,import-not-found]

REPO_ROOT = Path(__file__).resolve().parents[1]
ELECTRON_DIR_REL = "apps/desktop/electron"
FRONTEND_DIR_REL = "apps/webui/frontend"

# The single Open DJ app: the Electron shell packs with the Tauri app's name,
# bundle id and data dir (apps/desktop/electron/package.json), so an install
# replaces whichever shell's build is at /Applications/Open DJ.app.
APP_NAME = "Open DJ.app"
BUNDLE_ID = "com.opendj.desktop"
BUNDLE_DISPLAY_NAME = "Open DJ"
APPLICATIONS = "/Applications"
QUARANTINE_XATTR = "com.apple.quarantine"
#: launchctl print's answer for a service that was never bootstrapped (the
#: loop is not installed on that Mac), which is a real "not running".
PERSONA_NOT_INSTALLED = "Could not find service"
PACKED_APP_REL = f"{ELECTRON_DIR_REL}/out/mac-arm64/{APP_NAME}"
PAYLOAD_STAGE_REL = f"{ELECTRON_DIR_REL}/out/payload-stage"
SHELL_STAMP_IN_ASAR = "dist/build-identity.json"
STAMP_LANE = "electron"
STAMP_CHANNEL = "dev"

# Machines: name -> ssh host. The same three Macs as ship_dmg.sh's default
# rollout (OPS-50); agentbox is Linux and cannot run the app.
MACHINES = {"air": "air", "silver": "silver", "demon-llama": "demon-llama"}
# Every Mac in MACHINES may build: the owner rule that barred silver was
# lifted on Mon 2 Nov 2026. silver is now an M5 Max Mac Studio whose names
# (maintainer-Mac-Studio, "the maintainer's Mac Studio") carry no "silver". Checked in
# order, so demon-llama wins over a Studio-looking name.
LOCAL_NAME_HINTS = (
    ("demon-llama", "demon-llama"),
    ("silver", "silver"),
    ("mac-studio", "silver"),
    ("mac studio", "silver"),
    ("air", "air"),
)
SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
QUIT_TIMEOUT_S = 30
NO_CLOSE_WAIT_S = 180

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_SIGNING_UNIMPLEMENTED = 3

SIGNING_REFUSAL = (
    "[ERROR] signing needs the owner's Apple credentials: signing and notarizing the "
    "Electron app is not implemented yet (plan E2 stops before it). Nothing was "
    "built or installed. Re-run with --unsigned (or MDT_SHIP_UNSIGNED=1) for a "
    "deliberately unsigned build."
)


class ShipError(RuntimeError):
    """A step failed. The message names the step and what it saw."""


@dataclass
class Step:
    """One thing a live run does, and the line --dry-run prints for it."""

    title: str
    commands: list[str] = field(default_factory=list)
    action: Callable[[], None] | None = None


# ----- local identity ----------------------------------------------------


def local_machine_name(env: Mapping[str, str] | None = None) -> str:
    """The same answer as ship_dmg.sh's _local_machine_name, MDT_LOCAL_NAME first."""
    source: Mapping[str, str] = os.environ if env is None else env
    override = source.get("MDT_LOCAL_NAME", "").strip()
    if override:
        return override
    if platform.system() != "Darwin":
        return "other"
    names = [platform.node()]
    for key in ("ComputerName", "LocalHostName"):
        scutil = shutil.which("scutil")
        if scutil is None:
            break
        out = subprocess.run([scutil, "--get", key], capture_output=True, text=True, check=False)
        names.append(out.stdout.strip())
    joined = " ".join(names).lower()
    for hint, name in LOCAL_NAME_HINTS:
        if hint in joined:
            return name
    return "other"


# ----- app.asar ----------------------------------------------------------


def read_asar_file(asar: Path, inner: str) -> bytes:
    """Read one file out of an Electron ``app.asar`` archive.

    Format (electron/asar): a Chromium pickle holding the header size, then a
    pickle holding the JSON header string, then the file bytes. Offsets in the
    header are relative to the end of the header pickle.
    """
    with asar.open("rb") as handle:
        prefix = handle.read(16)
        if len(prefix) < 16:
            raise ShipError(f"{asar} is too short to be an asar archive")
        _size_pickle_payload, header_pickle_size, _payload, json_size = struct.unpack("<4I", prefix)
        header_json = handle.read(json_size)
        if len(header_json) != json_size:
            raise ShipError(f"{asar} header is truncated")
        try:
            header = json.loads(header_json.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ShipError(f"{asar} header is not JSON: {exc}") from exc
        node = header
        for part in inner.split("/"):
            files = node.get("files") if isinstance(node, dict) else None
            if not isinstance(files, dict) or part not in files:
                raise ShipError(f"{inner} is not in {asar}")
            node = files[part]
        if not isinstance(node, dict) or "offset" not in node or "size" not in node:
            raise ShipError(f"{inner} in {asar} is not a packed file")
        if node.get("unpacked"):
            raise ShipError(f"{inner} in {asar} is unpacked; read it from app.asar.unpacked")
        handle.seek(8 + header_pickle_size + int(node["offset"]))
        data = handle.read(int(node["size"]))
        if len(data) != int(node["size"]):
            raise ShipError(f"{inner} in {asar} is truncated")
        return data


# ----- stamps ------------------------------------------------------------


def verify_packed_app(app: Path, target_sha: str) -> list[str]:
    """Check the packed bundle against the target commit. Returns OK lines, raises on any mismatch."""
    plist_path = app / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise ShipError(f"cannot read {plist_path}: {exc}") from exc
    found_id = plist.get("CFBundleIdentifier")
    found_name = plist.get("CFBundleName")
    if found_id != BUNDLE_ID:
        raise ShipError(f"{plist_path} CFBundleIdentifier is {found_id!r}, expected {BUNDLE_ID!r}")
    if found_name != BUNDLE_DISPLAY_NAME:
        raise ShipError(f"{plist_path} CFBundleName is {found_name!r}, expected {BUNDLE_DISPLAY_NAME!r}")

    asar = app / "Contents" / "Resources" / "app.asar"
    try:
        shell_stamp = json.loads(read_asar_file(asar, SHELL_STAMP_IN_ASAR).decode("utf-8"))
        packed_pkg = json.loads(read_asar_file(asar, "package.json").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShipError(f"a stamp inside {asar} is not JSON: {exc}") from exc
    identifier = (packed_pkg.get("opendj") or {}).get("identifier")
    if identifier != BUNDLE_ID:
        raise ShipError(f"packed package.json opendj.identifier is {identifier!r}, expected {BUNDLE_ID!r}")
    if shell_stamp.get("git_sha_full") != target_sha:
        raise ShipError(
            f"shell stamp {SHELL_STAMP_IN_ASAR} says {shell_stamp.get('git_sha_full')!r}, target is {target_sha}"
        )

    manifest_path = app / "Contents" / "Resources" / "payload" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShipError(f"cannot read the payload manifest {manifest_path}: {exc}") from exc
    payload_sha = (manifest.get("identity") or {}).get("git_sha_full")
    if payload_sha != target_sha:
        raise ShipError(f"payload manifest says {payload_sha!r}, target is {target_sha}")
    return [
        f"[OK] bundle: {BUNDLE_DISPLAY_NAME} / {BUNDLE_ID}, data dir identifier {identifier}",
        f"[OK] shell stamp and payload manifest both {target_sha[:12]}",
    ]


def replace_placeholder_payload(app: Path, staged: Path) -> None:
    """Swap the pack's placeholder payload dir for the staged engine payload (ditto on macOS).

    The copy and its guards live in scripts/electron_payload.py so the Linux
    pack (``pnpm pack:dir:linux`` then ``python -m scripts.electron_payload``)
    runs the same code.
    """
    try:
        electron_payload.install_payload(app / "Contents" / "Resources", staged, "darwin")
    except electron_payload.PayloadInstallError as exc:
        raise ShipError(str(exc)) from exc


# ----- the plan ----------------------------------------------------------


@dataclass
class Options:
    dry_run: bool
    unsigned: bool
    sha: str
    machines: list[str]
    no_close: bool
    local: str


def parse_args(argv: Sequence[str], env: dict) -> Options:
    parser = argparse.ArgumentParser(
        prog="ship_dmg.sh --shell electron",
        description="Build the Electron app (unsigned) and install it as /Applications/Open DJ.app.",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan; run nothing")
    parser.add_argument(
        "--unsigned",
        action="store_true",
        help="build deliberately unsigned (also MDT_SHIP_UNSIGNED=1). Required: signing is not implemented.",
    )
    parser.add_argument(
        "--sha", default="HEAD", help="target git ref; the checkout must already be at it (default HEAD)"
    )
    parser.add_argument(
        "--machine", action="append", default=[], choices=sorted(MACHINES), help="repeatable; default all three Macs"
    )
    parser.add_argument(
        "--no-close", action="store_true", help="wait for the operator to quit the app instead of asking it to"
    )
    args, unknown = parser.parse_known_args(list(argv))
    if unknown:
        not_here = [a for a in unknown if a.split("=")[0] in ("--lane", "--build-on", "--requested-at")]
        if not_here:
            parser.exit(EXIT_USAGE, f"[ERROR] {not_here[0].split('=')[0]} is not supported with --shell electron\n")
        parser.exit(EXIT_USAGE, f"[ERROR] unrecognized arguments: {' '.join(unknown)}\n")
    machines = list(dict.fromkeys(args.machine)) or list(MACHINES)
    return Options(
        dry_run=args.dry_run,
        unsigned=args.unsigned or env.get("MDT_SHIP_UNSIGNED", "") == "1",
        sha=args.sha,
        machines=machines,
        no_close=args.no_close,
        local=local_machine_name(env),
    )


def _q(path: str) -> str:
    return shlex.quote(path)


def staging_dir(run_id: str) -> str:
    """Where the replacement bundle is built before it is swapped in: a sibling
    of the installed app, so the swap is a same-volume rename."""
    return f"{APPLICATIONS}/.opendj-ship-{run_id}"


def swap_command(installed: str, staging: str, populate: str) -> str:
    """Build the replacement in `staging` with `populate` (which must leave the
    bundle at staging/APP_NAME), prove it is a bundle, and only then rename the
    installed app aside and the new one into place. The previous app is kept
    at staging/previous.app and renamed back if the swap fails, so a full disk,
    a corrupt archive or a failed copy leaves the installed app as it was. On
    success it stays there too, until the post-install checks pass
    (cleanup_command) or fail (rollback_command).
    Exit 0 only when the new bundle is at `installed`: 80 staging could not be
    made, 81 populate failed, 82 the result is not a bundle, 83 the installed
    app could not be moved aside, 84 the swap failed and the previous app was
    put back, 85 the swap failed and so did the restore."""
    new, old = f"{staging}/{APP_NAME}", f"{staging}/previous.app"
    q_inst, q_stage, q_new, q_old = _q(installed), _q(staging), _q(new), _q(old)
    return (
        f"rm -rf {q_stage} && mkdir {q_stage} || exit 80; "
        f"{populate} || {{ rm -rf {q_stage}; exit 81; }}; "
        f"[ -f {_q(new + '/Contents/Info.plist')} ] && [ -f {_q(new + '/Contents/Resources/payload/manifest.json')} ]"
        f" || {{ rm -rf {q_stage}; exit 82; }}; "
        f"if [ -e {q_inst} ]; then mv {q_inst} {q_old} || {{ rm -rf {q_stage}; exit 83; }}; fi; "
        f"if mv {q_new} {q_inst}; then exit 0; fi; "
        f"if [ -e {q_old} ]; then mv {q_old} {q_inst} || exit 85; fi; exit 84"
    )


def rollback_command(installed: str, staging: str) -> str:
    """Put the previous app back after a post-install check failed: the
    rejected bundle goes to staging/failed.app and previous.app returns to
    `installed`. Exit 0 only when the previous app is back: 86 there was no
    previous app (a first install), 87 the rejected app could not be moved
    aside, 88 the previous app could not be moved back."""
    q_inst, q_old, q_failed = _q(installed), _q(f"{staging}/previous.app"), _q(f"{staging}/failed.app")
    return (
        f"[ -e {q_old} ] || exit 86; "
        f"rm -rf {q_failed}; mv {q_inst} {q_failed} || exit 87; "
        f"mv {q_old} {q_inst} || exit 88; "
        f"rm -rf {_q(staging)}; exit 0"
    )


def cleanup_command(staging: str) -> str:
    """Drop the staging sibling (and the previous app in it) once every
    post-install check has passed."""
    return f"rm -rf {_q(staging)}"


def populate_command(machine: str, local: str, archive: str, staging: str) -> str:
    """The copy that fills `staging`: ditto from the packed app here, or ditto
    out of the shipped archive over ssh (--keepParent puts APP_NAME at its root)."""
    if machine == local:
        return f"ditto {_q(str(REPO_ROOT / PACKED_APP_REL))} {_q(staging + '/' + APP_NAME)}"
    return f"ditto -x -k {_q(archive)} {_q(staging)}"


def archive_path(run_id: str) -> str:
    return f"/tmp/opendj-electron-{run_id}.zip"


@dataclass(frozen=True)
class InstallScript:
    """Every command one machine's install runs, built once so the dry-run plan
    prints the exact strings the live path executes (OPS-63). `ship` runs here;
    the rest run on the target, over ssh when it is not this machine."""

    ship: tuple[tuple[str, ...], ...]
    swap: str
    remove_archive: str | None
    quarantine: str
    shell_check: str
    manifest: str
    bundle_id: str
    rollback: str
    cleanup: str


def install_script(machine: str, local: str, run_id: str) -> InstallScript:
    installed = f"{APPLICATIONS}/{APP_NAME}"
    staging = staging_dir(run_id)
    archive = archive_path(run_id)
    remote = machine != local
    ship: tuple[tuple[str, ...], ...] = ()
    if remote:
        packed = str(REPO_ROOT / PACKED_APP_REL)
        ship = (
            ("ditto", "-c", "-k", "--keepParent", packed, archive),
            ("scp", "-q", *SSH_OPTS, archive, f"{MACHINES[machine]}:{archive}"),
        )
    return InstallScript(
        ship=ship,
        swap=swap_command(installed, staging, populate_command(machine, local, archive, staging)),
        remove_archive=f"rm -f {_q(archive)}" if remote else None,
        quarantine=quarantine_command(installed),
        shell_check=installed_shell_command(installed),
        manifest=f"cat {_q(installed + '/Contents/Resources/payload/manifest.json')}",
        bundle_id=f"/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' {_q(installed + '/Contents/Info.plist')}",
        rollback=rollback_command(installed, staging),
        cleanup=cleanup_command(staging),
    )


def install_commands(machine: str, local: str, run_id: str) -> list[str]:
    """The install as the plan prints it: the same strings `act()` executes,
    with the condition each one runs under. A target command is shown after
    `ssh <host> ` exactly as it is handed to ssh as the remote command."""
    script = install_script(machine, local, run_id)
    on = "" if machine == local else f"ssh {' '.join(SSH_OPTS)} {MACHINES[machine]} "
    lines = [" ".join(_q(arg) for arg in argv) for argv in script.ship]
    lines.append(f"{on}{script.swap}")
    if script.remove_archive:
        lines.append(f"{on}{script.remove_archive}  # always, after the swap; then the local archive is removed")
    lines += [
        f"{on}{script.quarantine}",
        f"{on}{script.shell_check}  # must print the verified packed app.asar sha256",
        f"{on}{script.manifest}  # identity.git_sha_full must be the target",
        f"{on}{script.bundle_id}",
        f"{on}{script.rollback}  # ONLY if the quarantine check or read-back failed; the run then fails",
        f"{on}{script.cleanup}  # only once the quarantine check and read-back passed",
    ]
    return lines


def resolve_tool(name: str, path: str | None = None) -> str:
    """The absolute path of `name` on PATH (or `path`), so no launch picks a
    program by whatever PATH a step inherits (AGENTS.md "Launch programs by
    resolved path"). A missing tool stops the run naming it."""
    found = shutil.which(name, path=path)
    if found is None:
        raise ShipError(f"{name} is not on PATH; this step needs it")
    return os.path.abspath(found)


def _resolved(argv: Sequence[str]) -> list[str]:
    return [resolve_tool(argv[0]), *argv[1:]]


class Runner:
    """Runs commands on the local machine or over ssh, echoing each one."""

    def __init__(self, local: str) -> None:
        self.local = local

    def shell(self, machine: str, command: str, check: bool = True) -> subprocess.CompletedProcess:
        local = machine == self.local
        argv = ["bash", "-c", command] if local else ["ssh", *SSH_OPTS, MACHINES[machine], command]
        return subprocess.run(_resolved(argv), capture_output=True, text=True, check=check)

    def local_run(self, argv: Sequence[str], env: dict | None = None, cwd: Path | None = None) -> None:
        print(f"[RUN] {' '.join(_q(a) for a in argv)}", flush=True)
        subprocess.run(_resolved(argv), check=True, env=env, cwd=str(cwd or REPO_ROOT))


def _git(*args: str) -> str:
    argv = _resolved(["git", "-C", str(REPO_ROOT), *args])
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout.strip()


class Build:
    """The build half of a live run: one method per step, on this machine."""

    def __init__(self, opts: Options, target_sha: str, runner: Runner) -> None:
        self.opts = opts
        self.target_sha = target_sha
        self.runner = runner
        self.stage = REPO_ROOT / PAYLOAD_STAGE_REL
        self.app = REPO_ROOT / PACKED_APP_REL

    def preflight(self) -> None:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise ShipError(f"building needs an arm64 Mac; this is {platform.system()}/{platform.machine()}")
        if self.opts.local not in MACHINES:
            raise ShipError(f"this machine ({self.opts.local}) is not a known Mac; run from the Air, silver or demon-llama")
        if _git("rev-parse", "HEAD") != self.target_sha:
            raise ShipError(
                f"the checkout is not at {self.target_sha[:12]}; check out the target first (this never moves a checkout)"
            )
        dirty = _git("status", "--porcelain")
        if dirty:
            raise ShipError(f"the checkout is dirty; the stamps would say git_dirty=1:\n{dirty}")
        # OPS-50's rule: an unreachable target stops the run BEFORE the build.
        for machine in self.opts.machines:
            _require_reachable(self.runner, machine)

    def build_spa(self) -> None:
        self.runner.local_run(["pnpm", "install", "--frozen-lockfile"], cwd=REPO_ROOT / FRONTEND_DIR_REL)
        self.runner.local_run(["pnpm", "run", "build"], cwd=REPO_ROOT / FRONTEND_DIR_REL)

    def build_payload(self) -> None:
        # The previous packed app must go BEFORE staging: the payload copies the
        # apps tree, so a leftover out/mac-arm64/Open DJ.app would be staged
        # into the new payload and fail its nested-installer check.
        packed_root = REPO_ROOT / ELECTRON_DIR_REL / "out" / "mac-arm64"
        if packed_root.exists():
            shutil.rmtree(packed_root)
        self.runner.local_run(
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
            ]
        )

    def pack_shell(self) -> None:
        env = {**os.environ, "OPENDJ_BUILD_LANE_LABEL": STAMP_LANE, "OPENDJ_BUILD_CHANNEL": STAMP_CHANNEL}
        env.pop("MDT_LANE_LABEL", None)
        self.runner.local_run(["pnpm", "--dir", ELECTRON_DIR_REL, "install", "--frozen-lockfile"])
        self.runner.local_run(["pnpm", "--dir", ELECTRON_DIR_REL, "pack:dir"], env=env)

    def copy_payload(self) -> None:
        replace_placeholder_payload(self.app, self.stage)

    def verify(self) -> None:
        for line in verify_packed_app(self.app, self.target_sha):
            print(line)


def build_plan(opts: Options, target_sha: str, run_id: str, runner: Runner) -> list[Step]:
    build = Build(opts, target_sha, runner)
    payload_in_app = _q(PACKED_APP_REL + "/Contents/Resources/payload")
    steps = [
        Step(
            "preflight: arm64 Mac that may build, clean checkout at the target, every target reachable",
            [
                f"git rev-parse HEAD  # must be {target_sha}",
                "git status --porcelain  # must be empty",
                *[f"ssh {m} echo opendj-ship-reachable" for m in opts.machines if m != opts.local],
            ],
            build.preflight,
        ),
        Step(
            "build the SPA (as ship_dmg.sh does before just dmg, OPS-53)",
            [f"cd {FRONTEND_DIR_REL} && pnpm install --frozen-lockfile && pnpm run build"],
            build.build_spa,
        ),
        Step(
            "build the engine payload (the just dmg payload step, plain label)",
            [
                f"rm -rf {ELECTRON_DIR_REL}/out/mac-arm64  # else the last packed app is staged into the payload",
                f"uv run --no-sync python -m scripts.build_engine_payload --out {PAYLOAD_STAGE_REL} --label ''",
            ],
            build.build_payload,
        ),
        Step(
            "pack the shell, unsigned",
            [
                f"pnpm --dir {ELECTRON_DIR_REL} install --frozen-lockfile",
                f"OPENDJ_BUILD_LANE_LABEL={STAMP_LANE} OPENDJ_BUILD_CHANNEL={STAMP_CHANNEL} pnpm --dir {ELECTRON_DIR_REL} pack:dir",
            ],
            build.pack_shell,
        ),
        Step(
            "replace the placeholder payload with the staged one",
            [
                f"rm -rf {payload_in_app}  # only if it holds just README.md",
                f"ditto {PAYLOAD_STAGE_REL} {payload_in_app}",
            ],
            build.copy_payload,
        ),
        Step(
            "verify the stamps",
            [
                f"Info.plist CFBundleIdentifier == {BUNDLE_ID}, CFBundleName == {BUNDLE_DISPLAY_NAME}",
                f"app.asar {SHELL_STAMP_IN_ASAR} git_sha_full == payload manifest git_sha_full == {target_sha}",
            ],
            build.verify,
        ),
    ]
    steps.extend(_install_step(machine, opts, target_sha, run_id, runner) for machine in opts.machines)
    return steps


def _install_step(machine: str, opts: Options, target_sha: str, run_id: str, runner: Runner) -> Step:
    installed = f"{APPLICATIONS}/{APP_NAME}"
    on = "" if machine == opts.local else f"ssh {' '.join(SSH_OPTS)} {MACHINES[machine]} "
    probe = f"{on}{app_running_command()}"
    if opts.no_close:
        quit_lines = [f"{probe}  # every 1s for up to {NO_CLOSE_WAIT_S}s while the operator quits it; never a signal"]
    else:
        quit_lines = [
            f"{on}{QUIT_COMMAND}  # never a signal",
            f"{probe}  # every 1s for up to {QUIT_TIMEOUT_S}s until it exits; still running fails the install",
        ]
    staging = staging_dir(run_id)
    script = install_script(machine, opts.local, run_id)
    commands = [
        f"{on}{PERSONA_PROBE}  # refuse while state = running (OPS-35)",
        f"{probe}  # exit 0 running, {APP_NOT_RUNNING_EXIT} not running (then no quit), anything else fails",
        *quit_lines,
        *install_commands(machine, opts.local, run_id),
    ]

    def act() -> None:
        _refuse_if_persona_active(runner, machine)
        _quit_installed(runner, machine, opts.no_close)
        archive = archive_path(run_id)
        for argv in script.ship:
            subprocess.run(_resolved(argv), check=True)
        try:
            _swap_in(runner, machine, script.swap, installed, staging)
        finally:
            if script.remove_archive:
                runner.shell(machine, script.remove_archive, check=False)
                os.remove(archive)
        try:
            _clear_quarantine(runner, machine, script.quarantine)
            _read_back(runner, machine, script, target_sha, packed_asar_sha256())
        except ShipError:
            _roll_back(runner, machine, script.rollback, installed, staging)
            raise
        cleanup = runner.shell(machine, script.cleanup, check=False)
        if cleanup.returncode != 0:
            print(f"[WARN] {machine}: installed and verified, but {staging} was not removed", flush=True)

    where = "this machine" if machine == opts.local else f"{machine} over ssh"
    return Step(f"install on {machine} ({where}) as {installed}", commands, act)


def _swap_in(runner: Runner, machine: str, swap: str, installed: str, staging: str) -> None:
    out = runner.shell(machine, swap, check=False)
    reasons = {
        80: f"could not create {staging}; the installed app is untouched",
        81: "the copy of the new app failed; the installed app is untouched",
        82: "the copied app has no Info.plist or payload manifest; the installed app is untouched",
        83: "the installed app could not be moved aside; it is untouched",
        84: f"the new app could not be moved into place; the previous app was put back at {installed}",
        85: f"the new app could not be moved into place AND the previous app could not be put back; it is at {staging}/previous.app",
    }
    if out.returncode != 0:
        why = reasons.get(out.returncode, f"the install exited {out.returncode} (UNKNOWN)")
        raise ShipError(f"{machine}: {why}: {(out.stderr or out.stdout).strip()}")


def _roll_back(runner: Runner, machine: str, rollback: str, installed: str, staging: str) -> None:
    out = runner.shell(machine, rollback, check=False)
    outcomes = {
        0: f"[ROLLBACK] {machine}: the previous app is back at {installed}; the rejected one was removed",
        86: f"[ROLLBACK] {machine}: no previous app to restore (first install); the rejected app stays at {installed}",
        87: f"[ROLLBACK] {machine}: FAILED, the rejected app could not be moved aside; the previous app is at {staging}/previous.app",
        88: f"[ROLLBACK] {machine}: FAILED, the previous app could not be moved back; it is at {staging}/previous.app",
    }
    print(
        outcomes.get(out.returncode, f"[ROLLBACK] {machine}: exited {out.returncode} (UNKNOWN); check {staging}"),
        flush=True,
    )


def quarantine_command(installed: str) -> str:
    """Clear quarantine, then PROVE it is gone. Exit 0 only when no file in the
    bundle still carries it: 90 xattr missing, 91 the bundle could not be
    listed, 92 quarantine survived. The delete itself may complain about files
    that never had the attribute, so its own exit is not the verdict."""
    target = _q(installed)
    return (
        "command -v xattr >/dev/null 2>&1 || exit 90; "
        f"xattr -dr {QUARANTINE_XATTR} {target} 2>/dev/null; "
        f"listing=$(xattr -r {target}) || exit 91; "
        f"printf '%s\\n' \"$listing\" | grep -qF {QUARANTINE_XATTR} && exit 92; "
        "exit 0"
    )


def _clear_quarantine(runner: Runner, machine: str, quarantine: str) -> None:
    out = runner.shell(machine, quarantine, check=False)
    reasons = {
        90: "xattr is not on PATH, so quarantine cannot be cleared",
        91: "the installed bundle could not be listed, so quarantine is UNVERIFIED",
        92: "quarantine is still set on the installed bundle, so the unsigned app will not launch",
    }
    if out.returncode != 0:
        why = reasons.get(out.returncode, f"the quarantine step exited {out.returncode} (UNKNOWN)")
        raise ShipError(f"{machine}: {why}: {(out.stderr or out.stdout).strip()}")


def _require_reachable(runner: Runner, machine: str) -> None:
    if machine == runner.local:
        return
    out = runner.shell(machine, "echo opendj-ship-reachable", check=False)
    if out.returncode != 0 or "opendj-ship-reachable" not in out.stdout:
        raise ShipError(
            f"{machine} cannot be reached over ssh ({(out.stderr or out.stdout).strip()}); narrow with --machine"
        )


PERSONA_PROBE = (
    "command -v launchctl >/dev/null 2>&1 || exit 90; launchctl print gui/$(id -u)/opendj-agt-persona-loop 2>&1"
)


def _refuse_if_persona_active(runner: Runner, machine: str) -> None:
    out = runner.shell(machine, PERSONA_PROBE, check=False)
    if out.returncode == 90:
        raise ShipError(f"{machine}: launchctl not found, so a running persona loop cannot be ruled out (UNKNOWN)")
    if out.returncode == 255:
        raise ShipError(f"{machine}: ssh failed while checking the persona loop (UNKNOWN): {out.stderr.strip()}")
    if "state = running" in out.stdout:
        raise ShipError(f"{machine}: REFUSING, opendj-agt-persona-loop is state=running; stop it or wait, then re-run")
    # Fail closed: only a readable print (exit 0) or launchd's own "no such
    # service" answer for a machine that never installed the loop counts as
    # not running. Any other exit means the probe did not measure.
    if out.returncode != 0 and PERSONA_NOT_INSTALLED not in out.stdout:
        raise ShipError(
            f"{machine}: the persona-loop probe exited {out.returncode} without an answer (UNKNOWN), "
            f"so a running loop cannot be ruled out: {(out.stdout or out.stderr).strip()}"
        )
    # A readable print names its state; an exit 0 with no state line (empty or
    # truncated output) did not measure either.
    if out.returncode == 0 and not any(line.strip().startswith("state = ") for line in out.stdout.splitlines()):
        raise ShipError(
            f"{machine}: the persona-loop probe printed no state line (UNKNOWN), "
            f"so a running loop cannot be ruled out: {out.stdout.strip()!r}"
        )


APP_NOT_RUNNING_EXIT = 3
QUIT_COMMAND = f"osascript -e 'tell application \"{BUNDLE_DISPLAY_NAME}\" to quit'"


def app_running_command() -> str:
    """Exit 0 when the installed app has a process, APP_NOT_RUNNING_EXIT only
    when `ps` itself succeeded and listed none; any other exit (90 no ps, 91 ps
    failed, 255 ssh) means the probe did not measure."""
    needle = _q(APPLICATIONS + "/" + APP_NAME + "/Contents/")
    return (
        "command -v ps >/dev/null 2>&1 || exit 90; "
        "procs=$(ps -axo comm=) || exit 91; "
        f"printf '%s\\n' \"$procs\" | grep -qF {needle} && exit 0; "
        f"exit {APP_NOT_RUNNING_EXIT}"
    )


def _app_running(runner: Runner, machine: str) -> bool:
    out = runner.shell(machine, app_running_command(), check=False)
    if out.returncode == 0:
        return True
    if out.returncode == APP_NOT_RUNNING_EXIT:
        return False
    raise ShipError(
        f"{machine}: the running-app probe exited {out.returncode} (UNKNOWN), so {BUNDLE_DISPLAY_NAME} "
        f"cannot be ruled out as running; refusing to replace it: {(out.stderr or out.stdout).strip()}"
    )


def _quit_installed(runner: Runner, machine: str, no_close: bool) -> None:
    if not _app_running(runner, machine):
        return
    if no_close:
        print(
            f"[ACTION] {machine}: QUIT {BUNDLE_DISPLAY_NAME} yourself now; waiting up to {NO_CLOSE_WAIT_S}s", flush=True
        )
        timeout = NO_CLOSE_WAIT_S
    else:
        runner.shell(machine, QUIT_COMMAND, check=False)
        timeout = QUIT_TIMEOUT_S
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _app_running(runner, machine):
            return
        time.sleep(1)
    if _app_running(runner, machine):
        raise ShipError(
            f"{machine}: {BUNDLE_DISPLAY_NAME} is still running after {timeout}s; refusing to install over it or to signal it"
        )


def packed_asar_sha256() -> str:
    """The sha256 of the packed app.asar, whose shell stamp the build step
    already verified against the target; the installed copy must match it."""
    asar = REPO_ROOT / PACKED_APP_REL / "Contents" / "Resources" / "app.asar"
    digest = hashlib.sha256()
    with asar.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def installed_shell_command(installed: str) -> str:
    """Print the installed app.asar's sha256 once the bundle's executable is
    proven present and executable. 90 shasum missing, 91 app.asar unreadable,
    92 Info.plist names no executable, 93 that executable is missing or not
    executable."""
    asar = _q(installed + "/Contents/Resources/app.asar")
    plist = _q(installed + "/Contents/Info.plist")
    macos = _q(installed + "/Contents/MacOS")
    return (
        "command -v shasum >/dev/null 2>&1 || exit 90; "
        f"sum=$(shasum -a 256 {asar}) || exit 91; "
        f"exe=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' {plist}) && [ -n \"$exe\" ] || exit 92; "
        f'[ -f {macos}/"$exe" ] && [ -x {macos}/"$exe" ] || exit 93; '
        "printf '%s\\n' \"${sum%% *}\""
    )


def _read_back(runner: Runner, machine: str, script: InstallScript, target_sha: str, packed_asar: str) -> None:
    installed = f"{APPLICATIONS}/{APP_NAME}"
    shell = runner.shell(machine, script.shell_check, check=False)
    reasons = {
        90: "shasum is not on PATH",
        91: "the installed app.asar cannot be read",
        92: "the installed Info.plist names no executable",
        93: "the installed executable is missing or not executable",
    }
    if shell.returncode != 0:
        why = reasons.get(shell.returncode, f"the shell check exited {shell.returncode} (UNKNOWN)")
        raise ShipError(f"{machine}: {why}; the install is UNVERIFIED: {(shell.stderr or shell.stdout).strip()}")
    if shell.stdout.strip() != packed_asar:
        raise ShipError(
            f"{machine}: the installed app.asar (sha256 {shell.stdout.strip()[:16]!r}) is not the verified packed one "
            f"({packed_asar[:16]}), so its shell stamp is UNVERIFIED"
        )
    manifest = runner.shell(machine, script.manifest, check=False)
    plist_id = runner.shell(machine, script.bundle_id, check=False)
    try:
        sha = json.loads(manifest.stdout)["identity"]["git_sha_full"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ShipError(f"{machine}: cannot read the installed manifest ({exc}); the install is UNVERIFIED") from exc
    if not isinstance(sha, str):
        raise ShipError(
            f"{machine}: the installed manifest's git_sha_full is {sha!r}, not a sha; the install is UNVERIFIED"
        )
    if sha != target_sha or plist_id.stdout.strip() != BUNDLE_ID:
        raise ShipError(
            f"{machine}: installed stamp {sha[:12]} / {plist_id.stdout.strip()!r} is not {target_sha[:12]} / {BUNDLE_ID}"
        )
    print(f"[OK] {machine}: {installed} at {sha[:12]}, {BUNDLE_ID}")


def print_plan(steps: Sequence[Step], opts: Options, target_sha: str) -> None:
    print(f"[PLAN] shell: electron, unsigned, target {target_sha}")
    print(f"[PLAN] this machine: {opts.local}; install targets: {', '.join(opts.machines)}")
    print(f"[PLAN] installs only {APPLICATIONS}/{APP_NAME}, replacing whichever shell built it; no other Open DJ bundle is quit or deleted")
    for number, step in enumerate(steps, 1):
        print(f"[PLAN] {number}. {step.title}")
        for command in step.commands:
            print(f"[PLAN]      $ {command}")
    print("[PLAN] not done: signing, notarization, relaunch, engine health, opendj install-cli")


def main(argv: Sequence[str] | None = None, env: dict | None = None) -> int:
    env = dict(os.environ) if env is None else env
    opts = parse_args(sys.argv[1:] if argv is None else argv, env)
    if not opts.unsigned:
        print(SIGNING_REFUSAL, file=sys.stderr)
        return EXIT_SIGNING_UNIMPLEMENTED
    try:
        target_sha = _git("rev-parse", "--verify", f"{opts.sha}^{{commit}}")
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] cannot resolve --sha {opts.sha}: {exc.stderr.strip()}", file=sys.stderr)
        return EXIT_FAILED
    run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f".{os.getpid()}"
    runner = Runner(opts.local)
    steps = build_plan(opts, target_sha, run_id, runner)
    print(
        "[WARN] UNSIGNED Electron build: Gatekeeper refuses it on a downloaded copy; quarantine is cleared on install."
    )
    print_plan(steps, opts, target_sha)
    if opts.dry_run:
        print("[DRY-RUN] nothing was built, deleted or installed")
        return 0
    for number, step in enumerate(steps, 1):
        print(f"[STEP {number}/{len(steps)}] {step.title}", flush=True)
        try:
            if step.action is not None:
                step.action()
        except (ShipError, subprocess.CalledProcessError, OSError) as exc:
            print(f"[ERROR] step {number} ({step.title}) FAILED: {exc}", file=sys.stderr)
            return EXIT_FAILED
    print(
        f"[DONE] {APP_NAME} installed on {', '.join(opts.machines)} at {target_sha[:12]}, unsigned. Launch it by hand."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
