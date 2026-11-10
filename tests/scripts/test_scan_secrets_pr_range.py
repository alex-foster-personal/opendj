"""PR gitleaks range must fail closed when there is nothing to scan.

[if] merge-base..head contains zero commits [then] scan_secrets exits non-zero and names the range,
[else stop].
[if] SECURITY_BASE_SHA is empty or whitespace [then] merge-base falls back to SECURITY_BASE_REF,
[else stop].
[if] SECURITY_BASE_SHA is a bogus non-empty ref [then] range resolution fails closed,
[else stop].
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.security.scan_secrets import FAIL, PrRangeError, ScanContext, _fail_pr_range, resolve_pr_commit_range

ROOT = Path(__file__).resolve().parents[2]
SCAN_SECRETS = ROOT / "scripts" / "security" / "scan_secrets.py"
SCAN_SECRETS_SH = ROOT / "scripts" / "security" / "scan_secrets.sh"
BOGUS_BASE = "deadbeef" * 5


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, message: str, content: str) -> str:
    (repo / "f.txt").write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "--quiet",
            "-m",
            message,
        ],
        check=True,
    )
    return _git("rev-parse", "HEAD", cwd=repo)


def _one_commit_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    head = _commit(repo, "only", "x\n")
    return repo, head


def _two_commit_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    base = _commit(repo, "first", "a\n")
    head = _commit(repo, "second", "b\n")
    return repo, base, head


def _ctx(
    repo: Path,
    work: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_sha: str | None,
    head_sha: str,
    base_ref: str | None = None,
) -> ScanContext:
    work.mkdir(parents=True, exist_ok=True)
    if base_sha is None:
        monkeypatch.delenv("SECURITY_BASE_SHA", raising=False)
    else:
        monkeypatch.setenv("SECURITY_BASE_SHA", base_sha)
    monkeypatch.setenv("SECURITY_HEAD_SHA", head_sha)
    if base_ref is not None:
        monkeypatch.setenv("SECURITY_BASE_REF", base_ref)
    return ScanContext(
        mode="pr",
        repo_root=repo,
        work_dir=work,
        bin_dir=work / "bin",
        summary_file=work / "summary.tsv",
        out=work / "secrets",
        gitleaks_config=ROOT / ".gitleaks.toml",
        git="git",
    )


def test_resolve_pr_range_zero_commits_same_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, head = _one_commit_repo(tmp_path)
    work = tmp_path / "work"
    ctx = _ctx(repo, work, monkeypatch, base_sha=head, head_sha=head)
    base, resolved_head, commits = resolve_pr_commit_range(ctx)
    assert base == head
    assert resolved_head == head
    assert commits == 0


def test_resolve_pr_range_empty_base_sha_falls_back_to_base_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing empty-base fallback (_env_stripped) must make this test fail (mutation check)."""
    repo, base, head = _two_commit_repo(tmp_path)
    work = tmp_path / "work"
    for empty in ("", "   "):
        ctx = _ctx(repo, work, monkeypatch, base_sha=empty, head_sha=head, base_ref=base)
        _, resolved_head, commits = resolve_pr_commit_range(ctx)
        assert resolved_head == head
        assert commits == 1


def test_resolve_pr_range_bogus_base_sha_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, head = _one_commit_repo(tmp_path)
    work = tmp_path / "work"
    ctx = _ctx(repo, work, monkeypatch, base_sha=BOGUS_BASE, head_sha=head)
    with pytest.raises(PrRangeError, match="cannot resolve merge-base"):
        resolve_pr_commit_range(ctx)


def test_scan_pr_pins_fail_closed_on_zero_commits() -> None:
    """Removing the zero-commit guard must make this test fail (mutation check)."""
    text = SCAN_SECRETS.read_text(encoding="utf-8")
    block = text.split("if commits == 0:", 1)[1].split("range_report", 1)[0]
    assert "_fail_pr_range" in block
    assert "return PASS" not in block
    assert "no commits to scan in range" in block


def test_scan_secrets_sh_skips_empty_base_sha_export() -> None:
    text = SCAN_SECRETS_SH.read_text(encoding="utf-8")
    assert 'export SECURITY_BASE_SHA="${SECURITY_BASE_SHA:-}"' not in text
    assert 'if [[ -n "${SECURITY_BASE_SHA:-}" ]]' in text


def test_fail_pr_range_writes_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, head = _one_commit_repo(tmp_path)
    work = tmp_path / "work"
    ctx = _ctx(repo, work, monkeypatch, base_sha=head, head_sha=head)
    short = f"{head[:9]}..{head[:9]}"
    rc = _fail_pr_range(
        ctx,
        "gitleaks",
        "fired(1)",
        f"no commits to scan in range {short} (control-only is not a PR scan)",
    )
    assert rc == FAIL
    row = (work / "summary.tsv").read_text(encoding="utf-8")
    assert "FAIL" in row
    assert "no commits to scan in range" in row
