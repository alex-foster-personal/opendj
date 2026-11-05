"""[if] a shell format commit is not layout-only shfmt output of its parent [then] prove fails it or says UNKNOWN, [else stop].

`prove` hands shell scripts to `scripts/format_proof_shell.py`, which parses both sides with the pinned
shfmt's `--to-json`, drops every position, and requires the rest of the tree, every heredoc and
here-string byte, the `-n` verdict and the shebang to match, and head to be shfmt -i 4 -ci of base.
Every test builds its own throwaway repository and runs the real shfmt, so the module skips where the
pinned shfmt is not on PATH (install it with `python -m scripts.format_proof_shell install`).

Regression lines:
  - if prove fails a commit that is exactly shfmt -i 4 -ci of its parent (if, case, heredoc) then broken
  - if prove passes a formatted script that also changes a word, an operator or a comment then broken
  - if prove passes a heredoc body re-indented under <<- then broken
  - if prove passes a layout change that is not shfmt -i 4 -ci (two-space indent) then broken
  - if prove passes a changed shebang line then broken
  - if prove passes when shfmt is missing or is another version then broken: it must be UNKNOWN
  - if prove passes a base that shfmt cannot parse then broken: it must be UNKNOWN
  - if prove skips an extensionless bash script, or proves an extensionless non-shell file, then broken
  - if ignore-revs fails a listed commit that is exactly shfmt's output of its parent then broken
  - if the heredoc and here-string bytes read back are not exactly the source's then broken
  - if prove crashes, rather than saying UNKNOWN for the path, when a head blob cannot be read then broken
  - if prove crashes, rather than saying UNKNOWN naming bash, when bash is not on PATH then broken
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import format_proof, format_proof_shell
from scripts.format_proof_shell import CFG
from tests.quality.format_proof_repo import commit_files, init_repo


def _pinned_shfmt_on_path() -> bool:
    binary = shutil.which("shfmt")
    if binary is None:
        return False
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False).stdout.strip()
    return version == f"v{CFG.SHFMT_VERSION}"


pytestmark = [
    pytest.mark.requirement("OPS-8904"),
    pytest.mark.skipif(not _pinned_shfmt_on_path(), reason=f"shfmt v{CFG.SHFMT_VERSION} not on PATH"),
]

SUBJECT = f"style(format): shfmt@{CFG.SHFMT_VERSION} scripts"
BEFORE = """#!/usr/bin/env bash
# deploy helper
set -euo pipefail
if [ -n "${1:-}" ]; then
  echo "arg: $1"   # trailing comment
fi
case "$2" in
a) echo one ;;
*) echo other;;
esac
cat <<EOF
  keep   this  spacing
EOF
read -r x <<<"  spaced  "
"""
AFTER = """#!/usr/bin/env bash
# deploy helper
set -euo pipefail
if [ -n "${1:-}" ]; then
    echo "arg: $1" # trailing comment
fi
case "$2" in
    a) echo one ;;
    *) echo other ;;
esac
cat <<EOF
  keep   this  spacing
EOF
read -r x <<<"  spaced  "
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def _reformat(repo: Path, before: dict[str, str | bytes], after: dict[str, str | bytes]) -> tuple[str, str]:
    return commit_files(repo, before, "init"), commit_files(repo, after, SUBJECT)


def test_prove_passes_shfmt_output_with_if_case_heredoc_and_here_string(repo: Path) -> None:
    base, head = _reformat(repo, {"scripts/a.sh": BEFORE}, {"scripts/a.sh": AFTER})
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 0, result.lines
    assert any(f"OK 1 shell scripts are layout-only, shfmt@{CFG.SHFMT_VERSION}" in line for line in result.lines)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('echo "arg: $1"', 'echo "arg: $2"'),
        ("set -euo pipefail", "set -eu"),
        ("# deploy helper", "# deploy tool"),
        ("*) echo other ;;", "*) echo other && true ;;"),
    ],
    ids=["word", "option", "comment", "operator"],
)
def test_prove_fails_a_real_edit_on_top_of_the_layout(repo: Path, old: str, new: str) -> None:
    base, head = _reformat(repo, {"scripts/a.sh": BEFORE}, {"scripts/a.sh": AFTER.replace(old, new)})
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1, result.lines
    assert any("FAIL scripts/a.sh" in line for line in result.lines), result.lines


def test_prove_fails_a_heredoc_reindented_under_dash_heredoc(repo: Path) -> None:
    before = "#!/usr/bin/env bash\ncat <<-EOF\n\tbody\nEOF\n"
    base, head = _reformat(repo, {"a.sh": before}, {"a.sh": before.replace("\tbody", "body")})
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1, result.lines


def test_prove_fails_a_layout_that_is_not_the_ratified_style(repo: Path) -> None:
    two_space = AFTER.replace('    echo "arg', '  echo "arg')
    base, head = _reformat(repo, {"a.sh": BEFORE}, {"a.sh": two_space})
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1, result.lines
    assert any("not shfmt@" in line for line in result.lines), result.lines


def test_prove_fails_a_changed_shebang(repo: Path) -> None:
    base, head = _reformat(repo, {"a.sh": BEFORE}, {"a.sh": AFTER.replace("#!/usr/bin/env bash", "#!/bin/bash")})
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 1, result.lines
    assert any("shebang" in line for line in result.lines), result.lines


def test_prove_is_unknown_when_shfmt_is_missing(repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    base, head = _reformat(repo, {"a.sh": BEFORE}, {"a.sh": AFTER})
    real_which = shutil.which
    monkeypatch.setattr(format_proof_shell.shutil, "which", lambda name: None if name == "shfmt" else real_which(name))
    monkeypatch.setattr(CFG, "INSTALL_DIR", tmp_path / "no-shfmt-here")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 2, result.lines
    assert any("UNKNOWN shfmt is not on PATH" in line and "install" in line for line in result.lines)


def test_prove_is_unknown_when_shfmt_is_another_version(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base, head = _reformat(repo, {"a.sh": BEFORE}, {"a.sh": AFTER})
    monkeypatch.setattr(CFG, "SHFMT_VERSION", "0.0.1")
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 2, result.lines
    assert any("not the pinned v0.0.1" in line for line in result.lines), result.lines


def test_prove_is_unknown_when_the_base_does_not_parse(repo: Path) -> None:
    broken = "#!/usr/bin/env bash\nif true; then\necho x\n"
    base, head = _reformat(repo, {"a.sh": broken}, {"a.sh": broken.replace("echo x", "    echo x")})
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 2, result.lines
    assert any("UNKNOWN a.sh: shfmt cannot parse" in line for line in result.lines), result.lines


def test_prove_routes_an_extensionless_bash_script_and_not_other_extensionless_files(repo: Path) -> None:
    base, head = _reformat(repo, {"bin/deploy": BEFORE}, {"bin/deploy": AFTER})
    assert format_proof.prove(repo, base, head).exit_code == 0
    base, head = _reformat(repo, {"bin/notes": "hello  world\n"}, {"bin/notes": "hello world\n"})
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 2, result.lines
    assert any("UNKNOWN bin/notes" in line for line in result.lines), result.lines


def test_ignore_revs_passes_a_listed_shfmt_commit(repo: Path) -> None:
    _, head = _reformat(repo, {"a.sh": BEFORE}, {"a.sh": AFTER})
    commit_files(repo, {format_proof.CFG.IGNORE_REVS_FILE: f"# shell part\n{head}\n"}, "chore: list it")
    result = format_proof.check_ignore_revs(repo, repo / format_proof.CFG.IGNORE_REVS_FILE)
    assert result.exit_code == 0, result.lines


def test_redirect_bodies_are_the_exact_source_bytes() -> None:
    source = b'x=$(cat <<<"a  b")\ncat <<-EOF\n\tbody\nEOF\ncat <<E2\nE2\n'
    shfmt = shutil.which("shfmt")
    assert shfmt is not None
    tree = format_proof_shell._parse(shfmt, "x.sh", source)
    assert format_proof_shell._redirect_bodies(tree, source, []) == [b'"a  b"', b"\tbody\nEOF", b""]


def test_prove_is_unknown_naming_the_path_when_a_head_blob_cannot_be_read(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, head = _reformat(repo, {"bin/deploy": BEFORE}, {"bin/deploy": AFTER})
    real_blob = format_proof._blob

    def unreadable(repo_: Path, rev: str, path: str) -> bytes:
        if path == "bin/deploy":
            raise subprocess.CalledProcessError(128, ["git", "show"], stderr=b"fatal: bad object")
        return real_blob(repo_, rev, path)

    monkeypatch.setattr(format_proof, "_blob", unreadable)
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 2, result.lines
    assert "[format-proof] UNKNOWN bin/deploy: git show exited 128: fatal: bad object" in result.lines


def test_prove_is_unknown_naming_bash_when_bash_is_not_on_path(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base, head = _reformat(repo, {"a.sh": BEFORE}, {"a.sh": AFTER})
    real_which = shutil.which
    monkeypatch.setattr(
        format_proof_shell.shutil, "which", lambda name: None if name in ("bash", "sh") else real_which(name)
    )
    result = format_proof.prove(repo, base, head)
    assert result.exit_code == 2, result.lines
    assert "[format-proof] UNKNOWN a.sh: bash is not on PATH, so its -n syntax check cannot run" in result.lines
