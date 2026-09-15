"""``opendj install-cli``: symlink the bundled launcher onto PATH without sudo.

Default target is ``~/.local/bin/opendj``. The command is idempotent when the
target already points at the payload's ``bin/opendj`` launcher, and refuses
loudly when something else occupies the path.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

from apps.opendj_cli import EXIT_FAILED

MANIFEST_KIND = "opendj-engine-payload"
CLI_LAUNCHER_NAME = "opendj"
DEFAULT_BIN_DIR = Path.home() / ".local" / "bin"
DEFAULT_TARGET = DEFAULT_BIN_DIR / CLI_LAUNCHER_NAME

_OUTSIDE_PAYLOAD = (
    "install-cli requires the installed app payload; run from "
    "Open DJ.app/Contents/Resources/payload/bin/opendj"
)


def payload_root() -> Path | None:
    """Return the payload root when this module runs inside a built payload."""
    current = Path(__file__).resolve().parent
    for parent in (current, *current.parents):
        manifest = parent / "manifest.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("kind") == MANIFEST_KIND:
            return parent
    return None


def bundled_launcher(root: Path | None = None) -> Path | None:
    payload = payload_root() if root is None else root
    if payload is None:
        return None
    return payload / "bin" / CLI_LAUNCHER_NAME


def _fail(*, as_json: bool, code: str, message: str, exit_code: int) -> int:
    if as_json:
        print(
            json.dumps(
                {"error": {"code": code, "message": message}, "exit_code": exit_code},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"opendj: {message}", file=sys.stderr)
    return exit_code


def _ok(*, as_json: bool, message: str, payload: dict[str, object] | None = None) -> int:
    if as_json:
        body: dict[str, object] = {"message": message, "exit_code": 0}
        if payload:
            body.update(payload)
        print(json.dumps(body, indent=2, sort_keys=True))
    else:
        print(message)
    return 0


def _parse_target(rest: Sequence[str]) -> tuple[Path, list[str]] | tuple[None, str]:
    target = DEFAULT_TARGET
    position = 0
    while position < len(rest):
        token = rest[position]
        if token == "--target":
            if position + 1 >= len(rest):
                return None, "install-cli --target requires a path"
            target = Path(rest[position + 1]).expanduser()
            position += 2
            continue
        return None, f"install-cli: unexpected argument {token!r}"
    return target, ""


def install(
    *,
    bundled: Path,
    target: Path,
) -> tuple[int, str, dict[str, object] | None]:
    if not bundled.is_file():
        return (
            EXIT_FAILED,
            f"install-cli: bundled launcher missing at {bundled}",
            {"bundled": str(bundled), "target": str(target)},
        )

    if target.exists() or target.is_symlink():
        if not target.is_symlink():
            return (
                EXIT_FAILED,
                (
                    f"install-cli: {target} exists and is not a symlink; "
                    f"refusing to overwrite (bundled launcher is {bundled})"
                ),
                {"bundled": str(bundled), "target": str(target)},
            )
        try:
            existing = target.resolve()
            desired = bundled.resolve()
        except OSError as error:
            return (
                EXIT_FAILED,
                f"install-cli: cannot resolve symlink paths: {error}",
                {"bundled": str(bundled), "target": str(target)},
            )
        if existing == desired:
            return (
                0,
                f"install-cli: already installed at {target} -> {bundled}",
                {"bundled": str(bundled), "target": str(target), "status": "already_installed"},
            )
        return (
            EXIT_FAILED,
            (
                f"install-cli: {target} points to {target.readlink()}, "
                f"not the bundled launcher at {bundled}; refusing to replace it"
            ),
            {"bundled": str(bundled), "target": str(target), "existing": str(existing)},
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(bundled)
    return (
        0,
        f"install-cli: installed {target} -> {bundled}",
        {"bundled": str(bundled), "target": str(target), "status": "installed"},
    )


def run(
    rest: Sequence[str],
    *,
    as_json: bool,
    bundled: Path | None = None,
    target: Path | None = None,
    payload: Path | None = None,
) -> int:
    if target is None:
        parsed_target, error = _parse_target(rest)
        if parsed_target is None:
            return _fail(as_json=as_json, code="usage", message=error, exit_code=EXIT_FAILED)
        target = parsed_target

    launcher = bundled if bundled is not None else bundled_launcher(payload)
    if launcher is None:
        return _fail(
            as_json=as_json,
            code="outside_payload",
            message=_OUTSIDE_PAYLOAD,
            exit_code=EXIT_FAILED,
        )

    exit_code, message, payload_body = install(bundled=launcher, target=target)
    if exit_code == 0:
        return _ok(as_json=as_json, message=message, payload=payload_body)
    return _fail(as_json=as_json, code="install_refused", message=message, exit_code=exit_code)
