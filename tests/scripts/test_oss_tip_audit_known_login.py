"""oss_tip_audit known-login rule: a login the tree reveals is caught written bare.

The positional home rules reported the tip clean at findings=0 while four tracked docs
named the nucbox WSL account as a bare word (Thu 1 Oct 2026). scripts/oss_tip_logins.py
harvests every login the published tree puts in a home-directory position, records
included, and reports a whole-word occurrence of one in any scanned file.

Regression lines:
- if a login revealed only by a write-once record is not reported bare in a doc then broken
- if a bare login is reported when nothing in the tree reveals it then broken (the
  rule must learn logins from the tree, not guess at words)
- if a placeholder login written bare is reported then broken (overshoot)
- if a login inside a longer identifier is reported then broken (overshoot)
- if the dash-encoded session path or a reverse-DNS label hides a login then broken
- if one home path is reported twice, by the home rule and this one, then broken
- if the reviewed bare-match exemptions also waive the home-path position then broken
- if the git-index walker (the real gate) skips the harvest then broken

Every login literal is assembled from fragments: the gate scans this file too.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.oss_tip_audit import audit_index, audit_paths
from scripts.oss_tip_logins import BARE_MATCH_EXEMPT, logins_in_text, revealed_logins

_HOME = "/" + "home" + "/"
_USERS = "/" + "Users" + "/"
_LOGIN = "zq" + "ruser"
_RECORD = "specs/session-record.md"
_DOC = "docs/ops/box.md"


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _findings(root: Path, files: dict[str, str]) -> list[tuple[str, int, str, str]]:
    paths = [_write(root, name, text) for name, text in files.items()]
    return [(f.path, f.line, f.rule, f.match) for f in audit_paths(root, paths).findings]


def _revealing_record() -> str:
    return f"ran it as `{_HOME}{_LOGIN}/jobs/x.sh`\n"


# ----- the miss this rule closes -------------------------------------------------------


def test_a_login_revealed_only_by_a_record_is_reported_bare(tmp_path: Path) -> None:
    files = {_RECORD: _revealing_record(), _DOC: f"intro\nreached as `{_LOGIN}` over ssh\n"}
    assert _findings(tmp_path, files) == [(_DOC, 2, "known-login", _LOGIN)]


def test_nothing_is_reported_when_the_tree_reveals_no_login(tmp_path: Path) -> None:
    """Negative control: the same bare word with no revealing record is just a word."""
    assert _findings(tmp_path, {_DOC: f"reached as `{_LOGIN}` over ssh\n"}) == []


def test_the_bare_rule_matches_case_insensitively(tmp_path: Path) -> None:
    files = {_RECORD: _revealing_record(), _DOC: f"user {_LOGIN.upper()}\n"}
    assert [rule for _, _, rule, _ in _findings(tmp_path, files)] == ["known-login"]


# ----- shapes the positional rules never had --------------------------------------------


def test_a_dash_encoded_session_path_is_reported(tmp_path: Path) -> None:
    encoded = f"/private/tmp/claude-1/-Users-{_LOGIN}-code-repo/scratch.wav"
    files = {_RECORD: _revealing_record(), _DOC: encoded + "\n"}
    assert [rule for _, _, rule, _ in _findings(tmp_path, files)] == ["known-login"]


def test_a_reverse_dns_launchd_label_is_reported(tmp_path: Path) -> None:
    files = {_RECORD: _revealing_record(), _DOC: f"launchctl print gui/501/com.{_LOGIN}.job\n"}
    assert [rule for _, _, rule, _ in _findings(tmp_path, files)] == ["known-login"]


# ----- overshoot controls ---------------------------------------------------------------


def test_a_placeholder_login_is_never_harvested(tmp_path: Path) -> None:
    files = {_RECORD: f"`{_HOME}dev/jobs` and `{_USERS}silver/x`\n", _DOC: "as `dev` on `silver`\n"}
    assert _findings(tmp_path, files) == []


def test_a_login_inside_a_longer_identifier_is_not_reported(tmp_path: Path) -> None:
    files = {_RECORD: _revealing_record(), _DOC: f"{_LOGIN}invisible and x_{_LOGIN} and {_LOGIN}2\n"}
    assert _findings(tmp_path, files) == []


def test_a_home_path_in_a_scanned_file_is_reported_once_by_its_own_rule(tmp_path: Path) -> None:
    files = {_DOC: f"see {_HOME}{_LOGIN}/jobs\n"}
    assert [rule for _, _, rule, _ in _findings(tmp_path, files)] == ["linux-home-path"]


def test_a_bare_match_exemption_still_reports_the_home_path_position(tmp_path: Path) -> None:
    exempt = sorted(BARE_MATCH_EXEMPT)[0]
    files = {_DOC: f"{exempt} said so\nsee {_HOME}{exempt}/jobs\n"}
    assert _findings(tmp_path, files) == [(_DOC, 2, "linux-home-path", f"{_HOME}{exempt}")]


def test_harvest_reduces_a_segment_with_trailing_noise_to_its_login() -> None:
    assert logins_in_text(f"{_USERS}{_LOGIN}...\n{_HOME}{_LOGIN}:x\n") == {_LOGIN}
    assert revealed_logins([f"{_USERS}dev\\n4."]) == frozenset()


# ----- the real gate --------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_the_git_index_gate_harvests_from_records(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _write(tmp_path, _RECORD, _revealing_record())
    _write(tmp_path, _DOC, f"as `{_LOGIN}`\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "x")
    result = audit_index(tmp_path)
    assert [(f.path, f.rule) for f in result.findings] == [(_DOC, "known-login")]
    assert result.records == 1, result.summary()
