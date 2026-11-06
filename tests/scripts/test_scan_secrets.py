"""Python secrets-scan orchestration.

[if] a git fixture contains a synthetic ghp_ token [then] the Python entry reports it.
[if] a git fixture is clean [then] the Python entry reports no findings.
[if] the control helper runs [then] it commits a fresh synthetic token.
[if] the shell wrapper changes [then] it remains a thin Python invoker.
"""

from __future__ import annotations

import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.security.scan_secrets import make_control_repo, scan_git_for_secrets

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts" / "security" / "scan_secrets.sh"
CONFIG = ROOT / ".gitleaks.toml"

GITLEAKS_STUB = r"""from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

report = Path(sys.argv[sys.argv.index("--report-path") + 1])
repo = Path(sys.argv[-1])
git = shutil.which("git")
if git is None:
    raise SystemExit(2)
tracked = subprocess.run(
    [git, "-C", str(repo), "ls-files"],
    check=True,
    capture_output=True,
    text=True,
).stdout.splitlines()
hit = next(
    (
        name
        for name in tracked
        if "ghp_" in (repo / name).read_text(encoding="utf-8")
    ),
    None,
)
doc = []
if hit is not None:
    doc.append(
        {
            "RuleID": "github-pat",
            "File": hit,
            "StartLine": 1,
            "Commit": "123456789abcdef",
        }
    )
report.write_text(json.dumps(doc), encoding="utf-8")
raise SystemExit(1 if doc else 0)
"""


def _git() -> str:
    found = shutil.which("git")
    assert found is not None
    return found


def _fixture_repo(tmp_path: Path, content: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = _git()
    subprocess.run([git, "init", "--quiet", str(repo)], check=True)
    (repo / "deploy.env").write_text(content, encoding="utf-8", newline="\n")
    subprocess.run([git, "-C", str(repo), "add", "deploy.env"], check=True)
    subprocess.run(
        [
            git,
            "-C",
            str(repo),
            "-c",
            "user.name=control",
            "-c",
            "user.email=control@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        check=True,
    )
    return repo


def _stub(tmp_path: Path) -> list[str]:
    stub = tmp_path / "gitleaks_stub.py"
    stub.write_text(GITLEAKS_STUB, encoding="utf-8", newline="\n")
    return [sys.executable, str(stub)]


def test_dirty_fixture_reports_synthetic_token(tmp_path: Path) -> None:
    token = f"ghp_{secrets.token_hex(18)}"
    repo = _fixture_repo(tmp_path, f"GITHUB_TOKEN={token}\n")
    findings = scan_git_for_secrets(
        repo,
        gitleaks_cmd=_stub(tmp_path),
        config=CONFIG,
        report=tmp_path / "dirty.json",
    )
    assert findings
    assert "github-pat deploy.env:1" in findings[0]


def test_clean_fixture_has_no_findings(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path, "HELLO=world\n")
    findings = scan_git_for_secrets(
        repo,
        gitleaks_cmd=_stub(tmp_path),
        config=CONFIG,
        report=tmp_path / "clean.json",
    )
    assert findings == []


def test_control_helper_commits_fresh_token(tmp_path: Path) -> None:
    repo = make_control_repo(tmp_path / "control")
    line = (repo / "deploy.env").read_text(encoding="utf-8").strip()
    assert line.startswith("GITHUB_TOKEN=")
    token = line.removeprefix("GITHUB_TOKEN=")
    assert re.fullmatch(r"ghp_[0-9a-f]{36}", token)
    result = subprocess.run(
        [_git(), "-C", str(repo), "rev-list", "--count", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert int(result.stdout) >= 1


def test_shell_wrapper_is_a_thin_python_invoker() -> None:
    text = WRAPPER.read_text(encoding="utf-8")
    assert "openssl" not in text
    assert "find " not in text
    assert "sed " not in text
    assert "python -m scripts.security.scan_secrets" in text
    assert "scan_doc_paths" in text
    assert 'source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"' in text
