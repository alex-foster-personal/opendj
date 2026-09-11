"""A stand-in for launchctl / systemctl / loginctl that really runs the units.

``tests/cloudsync/test_hub_script.py`` puts shims for these three commands
first on PATH so ``scripts/cloudsync_hub.sh`` can be exercised end to end on
any host without touching the user's real service manager. The stand-in is a
harness boundary, not a mock of the hub: ``bootstrap`` / ``enable --now``
read the unit file the script just rendered and START ITS COMMAND for real,
with only the environment the unit declares (plus PATH and HOME, as a
service manager would give it). So a unit that names the wrong command, a
wrong env, or an unreadable path fails the same way it would under launchd
or systemd.

Units that carry no start-at-load (the backup plist, the backup timer) are
only marked loaded: neither manager runs them at load time either.

State lives in ``$FAKE_SVC_STATE``: ``<label>.pid`` / ``<label>.loaded`` plus
``calls.jsonl`` (one line per invocation) so a test can prove a step was
reached. ``FAKE_SVC_MODE=record`` makes the start verbs record and exit 97
without starting anything, which lets a test stop the script right after
render.

Usage (from a shim): ``python fake_service_manager.py <launchctl|systemctl|loginctl> ARGS...``
"""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

RECORD_ONLY_EXIT: int = 97
NOT_LOADED_EXIT: int = 113
STOP_WAIT_S: float = 30.0


def _state_dir() -> Path:
    path = Path(os.environ["FAKE_SVC_STATE"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record(tool: str, args: list[str]) -> None:
    with (_state_dir() / "calls.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"tool": tool, "args": args}) + "\n")


def _pid_file(label: str) -> Path:
    return _state_dir() / f"{label}.pid"


def _loaded_file(label: str) -> Path:
    return _state_dir() / f"{label}.loaded"


def _alive(label: str) -> bool:
    pid_file = _pid_file(label)
    if not pid_file.is_file():
        return False
    try:
        os.kill(int(pid_file.read_text()), 0)
    except ProcessLookupError:
        return False
    return True


def _loaded(label: str) -> bool:
    return _loaded_file(label).is_file() or _alive(label)


def _spawn(label: str, argv: list[str], cwd: str, env: dict[str, str], log_path: Path) -> None:
    if os.environ.get("FAKE_SVC_MODE") == "record":
        sys.stderr.write(f"fake service manager: record-only, not starting {label}\n")
        raise SystemExit(RECORD_ONLY_EXIT)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    unit_env = {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"], **env}
    with log_path.open("ab") as log:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=unit_env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    _pid_file(label).write_text(str(proc.pid))
    _loaded_file(label).touch()


def _stop(label: str) -> None:
    if _alive(label):
        pid = int(_pid_file(label).read_text())
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + STOP_WAIT_S
        while _alive(label):
            if time.monotonic() > deadline:
                os.killpg(pid, signal.SIGKILL)
                raise SystemExit(f"fake service manager: {label} ignored SIGTERM")
            time.sleep(0.2)
    _pid_file(label).unlink(missing_ok=True)
    _loaded_file(label).unlink(missing_ok=True)


# ----- launchctl ---------------------------------------------------------------


def _launchctl(args: list[str]) -> int:
    verb = args[0]
    if verb == "print":
        label = args[1].rsplit("/", 1)[1]
        if not _loaded(label):
            return NOT_LOADED_EXIT
        print(f"gui/{os.getuid()}/{label} = {{\n\tstate = running\n}}")
        return 0
    if verb == "bootout":
        _stop(args[1].rsplit("/", 1)[1])
        return 0
    if verb == "bootstrap":
        plist = plistlib.loads(Path(args[2]).read_bytes())
        label = plist["Label"]
        if not plist.get("RunAtLoad"):
            _loaded_file(label).touch()
            return 0
        _spawn(
            label,
            list(plist["ProgramArguments"]),
            plist["WorkingDirectory"],
            dict(plist.get("EnvironmentVariables", {})),
            Path(plist["StandardOutPath"]),
        )
        return 0
    raise SystemExit(f"fake launchctl: unsupported verb {verb!r}")


# ----- systemctl / loginctl ----------------------------------------------------


def _unit_dir() -> Path:
    return Path(os.environ["XDG_CONFIG_HOME"]) / "systemd" / "user"


def _unit_values(text: str, key: str) -> list[str]:
    return [line.split("=", 1)[1] for line in text.splitlines() if line.startswith(f"{key}=")]


def _start_systemd_unit(unit: str) -> None:
    if not unit.endswith(".service"):
        _loaded_file(unit).touch()
        return
    text = (_unit_dir() / unit).read_text(encoding="utf-8")
    (exec_start,) = _unit_values(text, "ExecStart")
    (cwd,) = _unit_values(text, "WorkingDirectory")
    argv = [word.replace("%%", "%").replace("$$", "$") for word in shlex.split(exec_start)]
    env = dict(shlex.split(value)[0].split("=", 1) for value in _unit_values(text, "Environment"))
    _spawn(unit, argv, cwd, env, _state_dir() / f"{unit}.log")


def _systemctl(args: list[str]) -> int:
    if args[0] != "--user":
        raise SystemExit("fake systemctl: only --user is supported")
    verb, units = args[1], [a for a in args[2:] if not a.startswith("--")]
    if verb == "daemon-reload":
        return 0
    if verb == "enable":
        for unit in units:
            _start_systemd_unit(unit)
        return 0
    if verb == "disable":
        for unit in units:
            _stop(unit)
        return 0
    if verb == "is-active":
        active = _alive(units[0])
        print("active" if active else "inactive")
        return 0 if active else 3
    raise SystemExit(f"fake systemctl: unsupported verb {verb!r}")


def _loginctl(args: list[str]) -> int:
    if args[:1] == ["show-user"] and "Linger" in args:
        print("yes")
        return 0
    raise SystemExit(f"fake loginctl: unsupported call {args!r}")


TOOLS = {"launchctl": _launchctl, "systemctl": _systemctl, "loginctl": _loginctl}


def main(argv: list[str]) -> int:
    tool, args = argv[0], argv[1:]
    _record(tool, args)
    return TOOLS[tool](args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
