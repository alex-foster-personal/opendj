"""Persistent lint-imports tool environment for CI (issue #10884).

The import-contracts job runs the same non-isolated ``uv run`` invocation
``scripts.quality_gate._eval_arch`` uses, from a venv cached per runner and
keyed on ``ops/quality/requirements.txt`` plus the Python minor version.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL_REQS = REPO / "ops" / "quality" / "requirements.txt"
CACHE_KEY_FILE = "cache-key.txt"
VENV_DIR_NAME = ".venv"
CACHE_ROOT_ENV = "MDT_IMPORT_CONTRACTS_VENV"
DEFAULT_ROOT = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mdt-import-contracts"


def requirements_digest() -> str:
    return hashlib.sha256(TOOL_REQS.read_bytes()).hexdigest()


def config_identity(python_minor: str) -> str:
    """Directory name under the runner cache root."""
    return f"{requirements_digest()[:12]}-py{python_minor}"


def full_cache_key(python_minor: str) -> str:
    return f"{requirements_digest()}\npython={python_minor}\n"


def lint_imports_requirements_arg() -> str:
    """Requirements path in the same shape ``quality_gate._uv`` passes."""
    return str(TOOL_REQS)


def eval_arch_lint_imports_argv() -> list[str]:
    """Command argv matching ``quality_gate._uv`` for lint-imports (non-isolated)."""
    return [
        "uv",
        "run",
        "--no-project",
        "--quiet",
        "--with-requirements",
        lint_imports_requirements_arg(),
        "lint-imports",
        "--config",
        ".importlinter",
    ]


def resolve_cache_root(python_minor: str) -> Path:
    runner = os.environ.get("RUNNER_NAME", "local")
    return DEFAULT_ROOT / runner / config_identity(python_minor)


def _venv_python(python_minor: str) -> str:
    proc = subprocess.run(
        ["uv", "python", "find", "--system", python_minor],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    return proc.stdout.strip()


def provision_cache_root(root: Path, *, python_minor: str) -> Path:
    """Create or reuse the tool venv under ``root``; purge when the cache key drifts."""
    expected = full_cache_key(python_minor)
    key_file = root / CACHE_KEY_FILE
    venv = root / VENV_DIR_NAME

    if root.exists() and key_file.exists() and venv.exists():
        stored = key_file.read_text(encoding="utf-8").strip()
        if stored == expected and (venv / "bin" / "lint-imports").exists():
            root.chmod(0o700)
            return venv

    root.mkdir(parents=True, exist_ok=True)
    if venv.exists():
        shutil.rmtree(venv)

    interpreter = _venv_python(python_minor)
    subprocess.run(["uv", "venv", "--python", interpreter, str(venv)], check=True, cwd=REPO)
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv / "bin" / "python"),
            "-r",
            str(TOOL_REQS),
        ],
        check=True,
        cwd=REPO,
    )
    key_file.write_text(expected + "\n", encoding="utf-8")
    root.chmod(0o700)
    venv.chmod(0o700)
    return venv


def run_lint_imports(venv: Path) -> int:
    env = {k: v for k, v in os.environ.items()}
    env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    proc = subprocess.run(eval_arch_lint_imports_argv(), cwd=REPO, env=env)
    return proc.returncode


def _cmd_identity(args: argparse.Namespace) -> int:
    print(config_identity(args.python_minor))
    return 0


def _cmd_provision(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else resolve_cache_root(args.python_minor)
    provision_cache_root(root, python_minor=args.python_minor)
    print(root / VENV_DIR_NAME)
    return 0


def _cmd_run(_args: argparse.Namespace) -> int:
    venv_env = os.environ.get(CACHE_ROOT_ENV)
    if not venv_env:
        raise SystemExit(f"{CACHE_ROOT_ENV} is not set")
    venv = Path(venv_env)
    if not (venv / "bin" / "lint-imports").exists():
        raise SystemExit(f"lint-imports is missing from {venv}")
    return run_lint_imports(venv)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    identity = sub.add_parser("identity", help="print the cache directory identity")
    identity.add_argument("--python-minor", required=True)
    identity.set_defaults(func=_cmd_identity)

    provision = sub.add_parser("provision", help="create or reuse the cached tool venv")
    provision.add_argument("--python-minor", required=True)
    provision.add_argument("--root", help="cache root directory (defaults to runner layout)")
    provision.set_defaults(func=_cmd_provision)

    run = sub.add_parser("run", help="run lint-imports from MDT_IMPORT_CONTRACTS_VENV")
    run.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
