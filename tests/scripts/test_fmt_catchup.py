"""[if] a branch edits a file a format commit reformats [then] fmt_catchup merges it clean, [else stop]."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import fmt_catchup, format_proof_shell

pytestmark = pytest.mark.requirement("OPS-8756")

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "fmt_catchup.py"
IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com"}
IDENTITY |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

UNFORMATTED_A = "def f( a ):\n    return [a,1,2]\n" + "".join(
    f"\n\ndef g{i}( x ):\n    return {{'k':x}}\n" for i in range(8)
)
UNFORMATTED_B = "y = {'k':1}\n"


# ----- throwaway repository ---------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ | IDENTITY
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check, env=env)


def _commit(repo: Path, files: dict[str, str], subject: str) -> str:
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _ruff(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    command = ["uv", "run", "--isolated", "--no-project", "--python", "3.11"]
    command += ["--with-requirements", "ops/quality/requirements.txt", "ruff", "format", "--force-exclude", *args]
    return subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)


def _catchup(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ | IDENTITY
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=repo, capture_output=True, text=True, check=False, env=env
    )


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    """main with a format commit F over a.py and b.py, and branch feat (off F^) editing a.py."""
    if shutil.which("uv") is None:
        raise RuntimeError("uv is required: fmt_catchup runs the pinned ruff through it")
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    requirements = (ROOT / "ops/quality/requirements.txt").read_text()
    ruff_pin = next(line for line in requirements.splitlines() if line.startswith("ruff=="))
    base = {
        "pyproject.toml": "[tool.ruff]\nline-length = 120\n",
        "ops/quality/requirements.txt": ruff_pin + "\n",
        "a.py": UNFORMATTED_A,
        "b.py": UNFORMATTED_B,
    }
    _commit(repo, base, "base")
    _git(repo, "checkout", "-q", "-b", "feat")
    _commit(repo, {"a.py": UNFORMATTED_A.replace("[a,1,2]", "[a,1,2,3]"), "c.py": "z = ( 1 )\n"}, "feat edit")
    _git(repo, "checkout", "-q", "main")
    assert _ruff(repo, "--", "a.py", "b.py").returncode == 0
    fmt = _commit(repo, {}, "style(format): ruff test")
    _git(repo, "checkout", "-q", "feat")
    return repo, fmt


# ----- tests ------------------------------------------------------------


def test_plain_merge_of_the_format_commit_conflicts(repo: tuple[Path, str]) -> None:
    """Control: without the catch-up the branch's edit conflicts with the format commit."""
    path, fmt = repo
    assert _git(path, "merge", "--no-edit", fmt, check=False).returncode != 0
    _git(path, "merge", "--abort")


def test_catchup_merges_the_format_commit_and_keeps_the_branch_edit(repo: tuple[Path, str]) -> None:
    path, fmt = repo
    head_before = _git(path, "rev-parse", "HEAD").stdout.strip()
    result = _catchup(path, "--main", "main", "feat", fmt)
    assert result.returncode == 0, result.stderr + result.stdout
    assert _git(path, "merge-base", "--is-ancestor", fmt, "HEAD", check=False).returncode == 0
    assert _git(path, "merge-base", "--is-ancestor", head_before, "HEAD", check=False).returncode == 0
    assert "[a, 1, 2, 3]" in (path / "a.py").read_text()
    assert _ruff(path, "--check", "--", "a.py", "b.py").returncode == 0
    assert (path / "c.py").read_text() == "z = ( 1 )\n", (
        "a file the format commit never touched stays as the branch wrote it"
    )
    subjects = _git(path, "log", "--format=%s", f"{head_before}..HEAD").stdout.splitlines()
    assert any(s.startswith("style(format): catch-up ") for s in subjects)
    assert _git(path, "status", "--porcelain").stdout == ""


def test_catchup_is_a_no_op_when_the_branch_already_has_the_format_commit(repo: tuple[Path, str]) -> None:
    path, fmt = repo
    assert _catchup(path, "--main", "main", "feat", fmt).returncode == 0
    head = _git(path, "rev-parse", "HEAD").stdout.strip()
    second = _catchup(path, "--main", "main", "feat", fmt)
    assert second.returncode == 0 and "already in feat" in second.stdout
    assert _git(path, "rev-parse", "HEAD").stdout.strip() == head


def test_catchup_refuses_another_checked_out_branch(repo: tuple[Path, str]) -> None:
    path, fmt = repo
    result = _catchup(path, "--main", "main", "other", fmt)
    assert result.returncode == 1 and "check out 'other'" in result.stderr


def test_catchup_refuses_a_dirty_worktree(repo: tuple[Path, str]) -> None:
    path, fmt = repo
    (path / "a.py").write_text("dirty = 1\n")
    result = _catchup(path, "--main", "main", "feat", fmt)
    assert result.returncode == 1 and "uncommitted changes" in result.stderr


def test_catchup_stops_on_a_content_conflict_and_leaves_no_merge_in_progress(repo: tuple[Path, str]) -> None:
    path, _ = repo
    _git(path, "checkout", "-q", "main")
    _commit(path, {"b.py": "y = {'k':2}\n"}, "main content edit")
    assert _ruff(path, "--", "b.py").returncode == 0
    fmt = _commit(path, {}, "style(format): ruff b")
    _git(path, "checkout", "-q", "feat")
    _commit(path, {"b.py": "y = {'k':3}\n"}, "feat content edit")
    head_before = _git(path, "rev-parse", "HEAD").stdout.strip()
    result = _catchup(path, "--main", "main", "feat", fmt)
    assert result.returncode == 1 and "content conflict" in result.stderr
    assert _git(path, "rev-parse", "HEAD").stdout.strip() == head_before
    assert not (path / ".git" / "MERGE_HEAD").exists()


def test_catchup_follows_a_branch_rename_of_a_formatted_file(repo: tuple[Path, str]) -> None:
    path, fmt = repo
    _git(path, "mv", "a.py", "a2.py")
    _git(path, "commit", "-q", "-m", "rename a")
    result = _catchup(path, "--main", "main", "feat", fmt)
    assert result.returncode == 0, result.stderr + result.stdout
    assert not (path / "a.py").exists()
    assert "[a, 1, 2, 3]" in (path / "a2.py").read_text()
    assert _ruff(path, "--check", "--", "a2.py", "b.py").returncode == 0


def test_catchup_follows_a_main_rename_carried_in_by_the_parent_merge(tmp_path: Path) -> None:
    """Main renames a.py to b.py before formatting it: the branch's edit, merged into b.py, is caught up."""
    if shutil.which("uv") is None:
        raise RuntimeError("uv is required: fmt_catchup runs the pinned ruff through it")
    path = tmp_path / "r"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    requirements = (ROOT / "ops/quality/requirements.txt").read_text()
    ruff_pin = next(line for line in requirements.splitlines() if line.startswith("ruff=="))
    base = {"pyproject.toml": "[tool.ruff]\nline-length = 120\n", "ops/quality/requirements.txt": ruff_pin + "\n"}
    _commit(path, base | {"a.py": UNFORMATTED_A}, "base")
    _git(path, "checkout", "-q", "-b", "feat")
    _commit(path, {"a.py": UNFORMATTED_A.replace("[a,1,2]", "[a,1,2,3]")}, "feat edit")
    _git(path, "checkout", "-q", "main")
    _git(path, "mv", "a.py", "b.py")
    _git(path, "commit", "-q", "-m", "main renames a")
    assert _ruff(path, "--", "b.py").returncode == 0
    fmt = _commit(path, {}, "style(format): ruff b")
    _git(path, "checkout", "-q", "feat")
    result = _catchup(path, "--main", "main", "feat", fmt)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "1 branch file(s) caught up" in result.stdout
    assert "[a, 1, 2, 3]" in (path / "b.py").read_text()
    assert _ruff(path, "--check", "--", "b.py").returncode == 0


# ----- frontend (prettier) ----------------------------------------------

FRONTEND = "apps/webui/frontend"
REAL_FRONTEND = ROOT / FRONTEND
RATIFIED_PRETTIERRC = '{"useTabs": true, "singleQuote": true, "trailingComma": "none", "printWidth": 100}\n'
TS_UNFORMATTED = "export const a = {k:1}\n" + "".join(
    f"export function g{i}(x:number){{return {{k:x}}}}\n" for i in range(8)
)


@pytest.fixture
def fe_repo(tmp_path: Path) -> tuple[Path, str]:
    """main with a prettier format commit F over x.ts and y.ts, and branch feat (off F^) editing x.ts."""
    if not (REAL_FRONTEND / "node_modules" / "prettier").exists():
        pytest.skip("frontend node_modules not installed")
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    pin = json.loads((REAL_FRONTEND / "node_modules/prettier/package.json").read_text())["version"]
    base = {
        f"{FRONTEND}/.prettierrc": RATIFIED_PRETTIERRC,
        f"{FRONTEND}/package.json": json.dumps({"devDependencies": {"prettier": pin}}),
        f"{FRONTEND}/src/x.ts": TS_UNFORMATTED,
        f"{FRONTEND}/src/y.ts": "export const y = {k:1}\n",
        ".gitignore": "node_modules\n",
    }
    _commit(repo, base, "base")
    (repo / FRONTEND / "node_modules").symlink_to(REAL_FRONTEND / "node_modules")
    _git(repo, "checkout", "-q", "-b", "feat")
    _commit(repo, {f"{FRONTEND}/src/x.ts": TS_UNFORMATTED.replace("{k:1}", "{k:2}")}, "feat edit")
    _git(repo, "checkout", "-q", "main")
    fe = repo / FRONTEND
    prettier = ["./node_modules/.bin/prettier", "--write", "src/x.ts", "src/y.ts"]
    done = subprocess.run(prettier, cwd=fe, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    fmt = _commit(repo, {}, "style(format): prettier test")
    _git(repo, "checkout", "-q", "feat")
    return repo, fmt


def test_plain_merge_of_a_prettier_format_commit_conflicts(fe_repo: tuple[Path, str]) -> None:
    """Control: without the catch-up the branch's .ts edit conflicts with the prettier commit."""
    path, fmt = fe_repo
    assert _git(path, "merge", "--no-edit", fmt, check=False).returncode != 0
    _git(path, "merge", "--abort")


def test_catchup_formats_a_branch_ts_edit_with_the_pinned_prettier(fe_repo: tuple[Path, str]) -> None:
    path, fmt = fe_repo
    result = _catchup(path, "--main", "main", "feat", fmt)
    assert result.returncode == 0, result.stderr + result.stdout
    assert _git(path, "merge-base", "--is-ancestor", fmt, "HEAD", check=False).returncode == 0
    text = (path / FRONTEND / "src/x.ts").read_text()
    assert "export const a = { k: 2 };" in text, text
    check = subprocess.run(
        ["./node_modules/.bin/prettier", "--check", "src/x.ts", "src/y.ts"],
        cwd=path / FRONTEND,
        capture_output=True,
        check=False,
    )
    assert check.returncode == 0
    assert _git(path, "status", "--porcelain").stdout == ""


# ----- shell (shfmt) ----------------------------------------------------

UNFORMATTED_SH = "#!/usr/bin/env bash\nif true; then\n  echo one\nfi\n" + "".join(
    f"f{i}() {{\n  echo {i}\n}}\n" for i in range(8)
)


def _pinned_shfmt() -> bool:
    binary = shutil.which("shfmt")
    out = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False).stdout if binary else ""
    return out.strip() == f"v{format_proof_shell.CFG.SHFMT_VERSION}"


def test_catchup_shfmt_pin_matches_the_shell_proof() -> None:
    """fmt_catchup runs as a plain script, so it carries its own copy of the pin; the two must agree."""
    source = SCRIPT.read_text()
    assert f'SHFMT_VERSION = "{format_proof_shell.CFG.SHFMT_VERSION}"' in source
    flags = ", ".join(f'"{flag}"' for flag in format_proof_shell.CFG.SHFMT_FLAGS)
    assert f"SHFMT_FLAGS = ({flags})" in source


@pytest.mark.skipif(not _pinned_shfmt(), reason="pinned shfmt not on PATH")
def test_catchup_formats_a_branch_shell_edit_with_the_pinned_shfmt(tmp_path: Path) -> None:
    repo = tmp_path / "sh"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, {"run.sh": UNFORMATTED_SH, "tool": UNFORMATTED_SH}, "init")
    _git(repo, "checkout", "-q", "-b", "feat")
    _commit(
        repo,
        {
            "run.sh": UNFORMATTED_SH.replace("echo one", "echo branch-edit"),
            "tool": UNFORMATTED_SH + "f8() {\n  echo 8\n}\n",
        },
        "feat: edit",
    )
    _git(repo, "checkout", "-q", "main")
    shfmt = ["shfmt", "-w", *format_proof_shell.CFG.SHFMT_FLAGS, "run.sh", "tool"]
    assert subprocess.run(shfmt, cwd=repo, check=False).returncode == 0
    fmt = _commit(repo, {}, "style(format): shfmt test")
    _git(repo, "checkout", "-q", "feat")
    assert _git(repo, "merge", "--no-ff", "--no-edit", fmt, check=False).returncode != 0
    _git(repo, "merge", "--abort")
    done = _catchup(repo, "--main", "main", "feat", fmt)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "echo branch-edit" in (repo / "run.sh").read_text()
    assert "f8()" in (repo / "tool").read_text()
    check = ["shfmt", "-l", *format_proof_shell.CFG.SHFMT_FLAGS, "run.sh", "tool"]
    assert subprocess.run(check, cwd=repo, capture_output=True, check=False).returncode == 0


def test_verify_formatted_refuses_a_shfmt_listing_even_when_shfmt_exits_0(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `shfmt -l` that lists a file is unformatted whatever its exit code (review thread on #8904)."""
    listing = subprocess.CompletedProcess(["shfmt", "-l"], 0, stdout="run.sh\n", stderr="")
    monkeypatch.setattr(fmt_catchup, "_pinned_shfmt", lambda: "shfmt")
    monkeypatch.setattr(fmt_catchup.subprocess, "run", lambda *args, **kwargs: listing)
    with pytest.raises(fmt_catchup.CatchupError, match=r"unformatted files after merging abc: run\.sh"):
        fmt_catchup._verify_formatted(fmt_catchup.FilesToFormat(shell=["run.sh"]), "abc")


@pytest.mark.skipif(not _pinned_shfmt(), reason="pinned shfmt not on PATH")
def test_verify_formatted_refuses_an_unformatted_script_with_the_pinned_shfmt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "run.sh").write_text(UNFORMATTED_SH)
    (tmp_path / "ok.sh").write_text("#!/usr/bin/env bash\necho ok\n")
    monkeypatch.chdir(tmp_path)
    fmt_catchup._verify_formatted(fmt_catchup.FilesToFormat(shell=["ok.sh"]), "abc")
    with pytest.raises(fmt_catchup.CatchupError, match=r"after merging abc: run\.sh$"):
        fmt_catchup._verify_formatted(fmt_catchup.FilesToFormat(shell=["ok.sh", "run.sh"]), "abc")
