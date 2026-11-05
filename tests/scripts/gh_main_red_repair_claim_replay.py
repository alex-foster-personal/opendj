"""Stateful `gh` replay for ``ci_main_red_repair_claim`` race tests.

Installs a real executable named ``gh`` at the front of ``PATH`` so production
code resolves and execs it through ``shutil.which`` and ``subprocess`` -- no
``set_gh_runner`` monkeypatch. Comment list/post semantics follow GitHub ordering
(monotonic comment id, chronological replay for the parser).

-Claude
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

CONFIG_ENV = "MDT_MAIN_RED_REPAIR_GH_REPLAY_CONFIG"
MISS_EXIT = 92

_LAUNCHER = """#!{python}
import runpy
runpy.run_path({module}, run_name="__main__")
"""

_GH_VERSION = "gh version 2.64.0 (2024-12-01)"


def _load_state(config: dict) -> dict:
    path = Path(config["state_path"])
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    seed = config.get("seed_comments", [])
    state = {"comments": list(seed), "next_id": max((int(c.get("id", 0)) for c in seed), default=0) + 1}
    path.write_text(json.dumps(state), encoding="utf-8")
    return state


def _save_state(config: dict, state: dict) -> None:
    Path(config["state_path"]).write_text(json.dumps(state), encoding="utf-8")


def _record(log_path: Path, invocation: list[str]) -> None:
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(invocation) + "\n")


def _route(config: dict, args: list[str]) -> tuple[int, str, str]:
    if args == ["--version"]:
        return 0, _GH_VERSION + "\n", ""
    if args[:2] == ["auth", "status"]:
        return 0, "", ""
    if args[:1] != ["api"]:
        return 1, "", f"unsupported gh argv: {args}\n"
    state = _load_state(config)
    if args[1:3] == ["-X", "POST"] and "issues/" in args[3] and args[3].endswith("/comments"):
        for injected in config.get("inject_on_post", []):
            if not any(int(c.get("id", 0)) == int(injected["id"]) for c in state["comments"]):
                state["comments"].append(dict(injected))
        if state["comments"]:
            state["next_id"] = max(int(c.get("id", 0)) for c in state["comments"]) + 1
        body_field = next((a for a in args[4:] if a.startswith("body=")), None)
        if body_field is None:
            return 1, "", "missing body=\n"
        body = body_field.split("=", 1)[1]
        comment = {
            "id": state["next_id"],
            "created_at": f"2026-01-01T00:{state['next_id']:02d}:00Z",
            "body": body,
        }
        state["next_id"] += 1
        state["comments"].append(comment)
        _save_state(config, state)
        return 0, json.dumps(comment), ""
    if args[1].startswith("repos/") and args[1].endswith("/comments"):
        slurp = "--slurp" in args
        payload = state["comments"]
        if slurp:
            payload = [payload]
        return 0, json.dumps(payload), ""
    return 1, "", f"no replay route for: {args}\n"


def main(invoked_as: str, args: list[str]) -> int:
    config_path = os.environ.get(CONFIG_ENV)
    if not config_path:
        print(f"gh replay: {CONFIG_ENV} unset", file=sys.stderr)
        return MISS_EXIT
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    invocation = [invoked_as, *args]
    _record(Path(config["log_path"]), invocation)
    code, stdout, stderr = _route(config, args)
    if stderr:
        sys.stderr.write(stderr)
    if stdout:
        sys.stdout.write(stdout)
    return code


class MainRedRepairGhReplay:
    def __init__(self, log_path: Path, state_path: Path) -> None:
        self._log_path = log_path
        self._state_path = state_path

    @property
    def invocations(self) -> list[list[str]]:
        if not self._log_path.is_file():
            return []
        return [json.loads(line) for line in self._log_path.read_text(encoding="utf-8").splitlines() if line]

    @property
    def comments(self) -> list[dict[str, object]]:
        return json.loads(self._state_path.read_text(encoding="utf-8"))["comments"]


def install_main_red_repair_gh_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    seed_comments: list[dict[str, object]] | None = None,
    inject_on_post: list[dict[str, object]] | None = None,
) -> MainRedRepairGhReplay:
    import shutil

    workspace = tmp_path / "main-red-gh-replay"
    bin_dir = workspace / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    state_path = workspace / "state.json"
    log_path = workspace / "invocations.jsonl"
    config_path = workspace / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "state_path": str(state_path),
                "log_path": str(log_path),
                "seed_comments": seed_comments or [],
                "inject_on_post": inject_on_post or [],
            }
        ),
        encoding="utf-8",
    )
    module_path = Path(__file__).resolve()
    launcher_text = _LAUNCHER.format(
        python=sys.executable,
        module=json.dumps(str(module_path)),
    )
    posix_launcher = bin_dir / "gh"
    posix_launcher.write_text(launcher_text, encoding="utf-8")
    posix_launcher.chmod(posix_launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    win_launcher = bin_dir / "gh.cmd"
    win_launcher.write_text(
        f'@"{sys.executable}" "{module_path}" %*\r\n',
        encoding="utf-8",
    )
    expected_launcher = win_launcher if sys.platform == "win32" else posix_launcher
    monkeypatch.setenv(CONFIG_ENV, str(config_path))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_TOKEN", "replay-token")
    resolved = shutil.which("gh")
    if resolved != str(expected_launcher):
        raise AssertionError(
            f"PATH resolves gh to {resolved!r}, not replay at {expected_launcher!r}; refusing to reach live GitHub"
        )
    return MainRedRepairGhReplay(log_path, state_path)


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[0]).name, sys.argv[1:]))
