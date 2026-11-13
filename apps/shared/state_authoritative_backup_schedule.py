"""Opt-in OS scheduler installer for local state-authoritative backup.

Rendering helpers mirror ``apps.sync_hub.hub_deploy`` (systemd ExecStart quoting,
launchd ProgramArguments, ``__KEY__`` templates); they are duplicated here so
``apps.shared`` does not import ``apps.sync_hub``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from xml.sax.saxutils import escape

from apps.shared.source_tree import PAYLOAD_MANIFEST_ENV
from apps.shared.state_authoritative_backup import (
    DEFAULT_KEEP,
    StateAuthoritativeBackupError,
)

# Same relative path as ``PYTHON_LAUNCHER_RELATIVE`` in ``scripts/build_engine_payload.py``.
PAYLOAD_PYTHON_LAUNCHER_RELATIVE: str = "bin/opendj-python"

Kind = Literal["systemd", "launchd"]

BACKUP_HOUR: int = 3
BACKUP_MINUTE: int = 30
REGISTRY_FILENAME: str = "authoritative-backup-schedule.json"
TEMPLATES_DIR: Path = Path(__file__).resolve().parents[1] / "desktop" / "scheduling"

_TEMPLATE_FILES: dict[Kind, dict[str, str]] = {
    "systemd": {
        "service": "opendj-state-authoritative-backup.service.template",
        "timer": "opendj-state-authoritative-backup.timer.template",
    },
    "launchd": {
        "plist": "com.opendj.state-authoritative-backup.plist.template",
    },
}

_PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")


# ----- argv + quoting --------------------------------------------------------


def _is_payload_runtime_python(path: Path) -> bool:
    return path.name in ("python3", "python") and path.parent.name == "bin" and path.parent.parent.name == "runtime"


def _is_opendj_python_launcher(path: Path) -> bool:
    return path.name == "opendj-python" and path.parent.name == "bin"


def resolve_schedule_launcher(python: str, *, environ: Mapping[str, str] | None = None) -> str:
    """Map a packaged payload runtime python to ``bin/opendj-python`` for scheduled argv."""
    env = os.environ if environ is None else environ
    resolved = Path(python).resolve()
    if _is_opendj_python_launcher(resolved):
        if not resolved.is_file():
            raise StateAuthoritativeBackupError(f"opendj-python launcher is missing: {resolved}")
        return str(resolved)
    if _is_payload_runtime_python(resolved):
        payload_root = resolved.parent.parent.parent
        launcher = (payload_root / PAYLOAD_PYTHON_LAUNCHER_RELATIVE).resolve()
        if not launcher.is_file():
            raise StateAuthoritativeBackupError(
                f"payload launcher not found at {launcher}; build or stage the payload before scheduling backups"
            )
        return str(launcher)
    manifest = str(env.get(PAYLOAD_MANIFEST_ENV, "")).strip()
    if manifest and resolved == Path(sys.executable).resolve():
        from_manifest = (Path(manifest).resolve().parent / PAYLOAD_PYTHON_LAUNCHER_RELATIVE).resolve()
        if from_manifest.is_file():
            return str(from_manifest)
    return python


def state_authoritative_backup_argv(
    *,
    python: str,
    data_dir: Path,
    dest: Path | None = None,
    keep: int = DEFAULT_KEEP,
) -> list[str]:
    """Argv a scheduled job runs; must match an explicit ``backup`` invocation."""
    resolved = Path(data_dir).resolve()
    argv = [
        python,
        "-m",
        "apps.shared.state_authoritative_backup",
        "backup",
        "--data-dir",
        str(resolved),
        "--keep",
        str(keep),
    ]
    if dest is not None:
        argv.extend(["--dest", str(Path(dest).resolve())])
    return argv


def _systemd_word(arg: str) -> str:
    escaped = arg.replace("%", "%%").replace("$", "$$")
    if re.search(r'[\s"\\\']', escaped):
        return '"' + escaped.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return escaped


def systemd_exec_line(argv: Sequence[str]) -> str:
    return " ".join(_systemd_word(arg) for arg in argv)


def launchd_strings(argv: Sequence[str]) -> str:
    return "\n".join(f"\t\t<string>{escape(arg)}</string>" for arg in argv)


def render_template(template: str, values: Mapping[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        token = f"__{key}__"
        if token not in rendered:
            raise StateAuthoritativeBackupError(f"template has no {token} placeholder")
        rendered = rendered.replace(token, value)
    leftover = sorted(set(_PLACEHOLDER.findall(rendered)))
    if leftover:
        raise StateAuthoritativeBackupError(f"template placeholders left unfilled: {leftover}")
    return rendered


# ----- identity + registry ---------------------------------------------------


def _label_suffix(data_dir: Path) -> str:
    digest = hashlib.sha256(str(Path(data_dir).resolve()).encode()).hexdigest()
    return digest[:12]


def schedule_names(data_dir: Path) -> dict[str, str]:
    suffix = _label_suffix(data_dir)
    base = f"opendj-state-authoritative-backup-{suffix}"
    label = f"com.opendj.state-authoritative-backup.{suffix}"
    return {
        "suffix": suffix,
        "service": f"{base}.service",
        "timer": f"{base}.timer",
        "plist": f"{label}.plist",
        "label": label,
    }


def registry_path(data_dir: Path) -> Path:
    return Path(data_dir).resolve() / "state" / REGISTRY_FILENAME


@dataclass(frozen=True)
class ScheduleRegistry:
    data_dir: str
    kind: Kind
    label: str
    argv: tuple[str, ...]
    dest: str | None
    keep: int
    installed_at: str


def _plan_tuple(
    *, kind: Kind, argv: Sequence[str], dest: Path | None, keep: int
) -> tuple[Kind, tuple[str, ...], str | None, int]:
    dest_s = str(Path(dest).resolve()) if dest is not None else None
    return (kind, tuple(argv), dest_s, keep)


def _read_registry(data_dir: Path) -> ScheduleRegistry | None:
    path = registry_path(data_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        argv = payload.get("argv")
        if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
            raise StateAuthoritativeBackupError(f"{path} has invalid argv")
        return ScheduleRegistry(
            data_dir=str(payload["data_dir"]),
            kind=payload["kind"],
            label=str(payload["label"]),
            argv=tuple(argv),
            dest=payload.get("dest"),
            keep=int(payload["keep"]),
            installed_at=str(payload["installed_at"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StateAuthoritativeBackupError(f"{path} is not a usable schedule registry: {exc}") from exc


def _write_registry(path: Path, record: ScheduleRegistry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = asdict(record)
    payload["argv"] = list(record.argv)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _assert_writable_data_dir(data_dir: Path) -> Path:
    resolved = Path(data_dir).resolve()
    if not resolved.exists():
        raise StateAuthoritativeBackupError(f"data directory does not exist: {resolved}; create it before scheduling")
    if not os.access(resolved, os.W_OK):
        raise StateAuthoritativeBackupError(f"data directory is not writable: {resolved}")
    logs = resolved / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    return resolved


def default_kind() -> Kind:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    raise StateAuthoritativeBackupError(
        "Windows Task Scheduler is not implemented for state-authoritative backup; "
        "run backup manually or use a Linux or macOS host"
    )


def default_unit_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME") or Path.home())
    if sys.platform == "darwin":
        return home / "Library" / "LaunchAgents"
    xdg = env.get("XDG_CONFIG_HOME", "")
    return (Path(xdg) if xdg else home / ".config") / "systemd" / "user"


def _check_prior_registry(
    data_dir: Path,
    *,
    kind: Kind,
    argv: Sequence[str],
    dest: Path | None,
    keep: int,
    label: str,
) -> None:
    existing = _read_registry(data_dir)
    if existing is None:
        return
    resolved = str(data_dir)
    if existing.data_dir != resolved:
        raise StateAuthoritativeBackupError(
            f"a schedule registry already exists for {existing.data_dir!r}; "
            f"refusing a second registration for {resolved!r}"
        )
    plan = _plan_tuple(kind=kind, argv=argv, dest=dest, keep=keep)
    prior = _plan_tuple(
        kind=existing.kind, argv=existing.argv, dest=Path(existing.dest) if existing.dest else None, keep=existing.keep
    )
    if plan != prior:
        raise StateAuthoritativeBackupError(
            f"a different schedule is already registered for {resolved} "
            f"(label {existing.label!r}); remove it or match argv, keep, dest, and kind"
        )
    if existing.label != label:
        raise StateAuthoritativeBackupError(f"registry label {existing.label!r} does not match plan label {label!r}")


# ----- render ----------------------------------------------------------------


def render_schedule_units(
    *,
    kind: Kind,
    data_dir: Path,
    argv: Sequence[str],
    names: Mapping[str, str],
) -> dict[str, str]:
    logs = str(Path(data_dir) / "logs")
    data = str(Path(data_dir))
    on_calendar = f"*-*-* {BACKUP_HOUR:02d}:{BACKUP_MINUTE:02d}:00 UTC"
    if kind == "systemd":
        service_tpl = (TEMPLATES_DIR / _TEMPLATE_FILES["systemd"]["service"]).read_text(encoding="utf-8")
        timer_tpl = (TEMPLATES_DIR / _TEMPLATE_FILES["systemd"]["timer"]).read_text(encoding="utf-8")
        return {
            names["service"]: render_template(
                service_tpl,
                {
                    "DATA_DIR": data,
                    "LOG_DIR": logs,
                    "EXEC_START": systemd_exec_line(argv),
                },
            ),
            names["timer"]: render_template(timer_tpl, {"ON_CALENDAR": on_calendar}),
        }
    if kind == "launchd":
        plist_tpl = (TEMPLATES_DIR / _TEMPLATE_FILES["launchd"]["plist"]).read_text(encoding="utf-8")
        return {
            names["plist"]: render_template(
                plist_tpl,
                {
                    "LABEL": names["label"],
                    "DATA_DIR": escape(data),
                    "LOG_DIR": escape(logs),
                    "PROGRAM_ARGUMENTS": launchd_strings(argv),
                    "HOUR": str(BACKUP_HOUR),
                    "MINUTE": str(BACKUP_MINUTE),
                },
            ),
        }
    raise StateAuthoritativeBackupError(f"unknown schedule kind {kind!r}")


@dataclass(frozen=True)
class ScheduleInstallPlan:
    data_dir: str
    kind: Kind
    label: str
    argv: list[str]
    dest: str | None
    keep: int
    unit_dir: str
    unit_paths: dict[str, str]
    enable_commands: list[str]
    dry_run: bool


def enable_commands_for(kind: Kind, names: Mapping[str, str], unit_dir: Path) -> list[str]:
    if kind == "systemd":
        timer = names["timer"]
        return [f"systemctl --user enable --now {timer}"]
    plist = unit_dir / names["plist"]
    return [f"launchctl bootstrap gui/$UID {plist}"]


def schedule_install(
    *,
    data_dir: Path,
    python: str,
    dest: Path | None = None,
    keep: int = DEFAULT_KEEP,
    kind: Kind | None = None,
    unit_dir: Path | None = None,
    dry_run: bool = False,
) -> ScheduleInstallPlan:
    if keep < 1:
        raise StateAuthoritativeBackupError(f"--keep must be at least 1, got {keep}")
    resolved = _assert_writable_data_dir(data_dir)
    chosen_kind = default_kind() if kind is None else kind
    if sys.platform == "win32":
        raise StateAuthoritativeBackupError("Windows Task Scheduler is not implemented for state-authoritative backup")
    names = schedule_names(resolved)
    launcher = resolve_schedule_launcher(python)
    argv = state_authoritative_backup_argv(python=launcher, data_dir=resolved, dest=dest, keep=keep)
    _check_prior_registry(resolved, kind=chosen_kind, argv=argv, dest=dest, keep=keep, label=names["label"])
    units = render_schedule_units(kind=chosen_kind, data_dir=resolved, argv=argv, names=names)
    target_dir = default_unit_dir() if unit_dir is None else Path(unit_dir)
    unit_paths = {name: str(target_dir / name) for name in units}
    enables = enable_commands_for(chosen_kind, names, target_dir)
    plan = ScheduleInstallPlan(
        data_dir=str(resolved),
        kind=chosen_kind,
        label=names["label"],
        argv=argv,
        dest=str(Path(dest).resolve()) if dest is not None else None,
        keep=keep,
        unit_dir=str(target_dir),
        unit_paths=unit_paths,
        enable_commands=enables,
        dry_run=dry_run,
    )
    if dry_run:
        return plan
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, text in units.items():
        (target_dir / name).write_text(text, encoding="utf-8")
    installed_at = datetime.now(UTC).isoformat()
    _write_registry(
        registry_path(resolved),
        ScheduleRegistry(
            data_dir=str(resolved),
            kind=chosen_kind,
            label=names["label"],
            argv=tuple(argv),
            dest=plan.dest,
            keep=keep,
            installed_at=installed_at,
        ),
    )
    return plan


def schedule_inspect(*, data_dir: Path) -> dict[str, Any]:
    resolved = Path(data_dir).resolve()
    record = _read_registry(resolved)
    if record is None:
        raise StateAuthoritativeBackupError(
            f"no schedule registry at {registry_path(resolved)}; run schedule-install first"
        )
    names = schedule_names(resolved)
    unit_dir = default_unit_dir()
    if record.kind == "systemd":
        unit_paths = {
            names["service"]: str(unit_dir / names["service"]),
            names["timer"]: str(unit_dir / names["timer"]),
        }
    else:
        unit_paths = {names["plist"]: str(unit_dir / names["plist"])}
    return {
        "installed": True,
        "data_dir": record.data_dir,
        "kind": record.kind,
        "label": record.label,
        "argv": list(record.argv),
        "dest": record.dest,
        "keep": record.keep,
        "installed_at": record.installed_at,
        "registry_path": str(registry_path(resolved)),
        "unit_paths": unit_paths,
    }
