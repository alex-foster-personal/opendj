"""Throwaway git repositories for the format-proof tests.

Every test builds its own repository under pytest's tmp_path, so nothing depends on
this checkout's history or on whether CI cloned it shallow. Shared by `test_format_proof.py`,
`test_format_proof_docstrings.py`, `test_format_proof_regions.py` and `test_format_proof_ignore_revs.py`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

IDENTITY = ["-c", "user.email=t@example.com", "-c", "user.name=t"]


def init_repo(path: Path) -> Path:
    path.mkdir()
    run_git(path, "init", "-q", "-b", "main")
    return path


def run_git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit_files(repo: Path, files: dict[str, str | bytes], subject: str) -> str:
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            (repo / rel).write_bytes(content)
        elif isinstance(content, str):
            (repo / rel).write_text(content)
    run_git(repo, "add", "-A")
    run_git(repo, *IDENTITY, "commit", "-q", "-m", subject)
    return run_git(repo, "rev-parse", "HEAD")


def hash_blob(repo: Path, content: str) -> str:
    command = ["git", "-C", str(repo), "hash-object", "-w", "--stdin"]
    return subprocess.run(command, input=content, capture_output=True, text=True, check=True).stdout.strip()


def commit_index_entries(repo: Path, entries: dict[str, tuple[str, str]], subject: str) -> str:
    """Commit {path: (mode, object)} through the index: a working tree cannot make every mode on every OS."""
    for rel, (mode, object_sha) in entries.items():
        run_git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{object_sha},{rel}")
    run_git(repo, *IDENTITY, "commit", "-q", "-m", subject)
    return run_git(repo, "rev-parse", "HEAD")
