"""Checksum-bound mailbox exemptions for reviewed upstream license mirrors.

[if] those reviewed license blobs match their pins [then] mailboxes stay exempt, [else stop].

Regression lines:
- if a pinned license-mirror mailbox fails the publication gate then broken
- if the exemption drops matches instead of counting them then broken
- if altered published bytes at those paths stay exempt then broken
- if the same bytes at an unlisted path stay exempt then broken
- if a home path, CGNAT address or extra mailbox in a disposable copy stays quiet then broken
- if audit_index reads working-tree bytes for the pin then broken
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from scripts.license_mirrors import REVIEWED_LICENSE_TEXTS
from scripts.oss_tip_audit import (
    GENERATED_TEST_ID_PATHS,
    MAILBOX_EXEMPT_PATHS,
    UPSTREAM_LICENSE_MAILBOX_BLOBS,
    Finding,
    audit_index,
    findings_in_text,
    is_exempt,
    published_blobs,
)

pytestmark = pytest.mark.requirement("OSSPUB-01")

REPO_ROOT = Path(__file__).resolve().parents[2]

_EXPECTED_MAILBOX_LINES = {
    "docs/legal/lukeed-MIT.txt": (3,),
    "docs/legal/objc2-licenses.txt": (39,),
    "docs/legal/realfft-3.5.0-LICENSE.txt": (5,),
    "docs/legal/rollup-LICENSE.md": (24, 105, 152, 198, 244, 284, 446, 515, 532, 602),
}

_USERS = "/" + "Users" + "/"
_LOGIN = "jdoe"
_REAL_HOME = _USERS + _LOGIN + "/Music"
_CGNAT = "100." + "64.3.9"
_MAILBOX = "someone" + "@" + "not-a-real-provider" + ".com"


def _published_blob(rel: str) -> bytes:
    matches = [raw for path, _mode, raw in published_blobs(REPO_ROOT) if path == rel]
    assert len(matches) == 1, f"{rel}: expected one published blob, got {len(matches)}"
    return matches[0]


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "core.autocrlf", "false"],
        check=True,
    )
    return tmp_path


def _stage(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    subprocess.run(
        ["git", "-C", str(root), "add", "--", rel],
        check=True,
        capture_output=True,
    )


def _mailbox_hits(rel: str, raw: bytes) -> tuple[Finding, ...]:
    text = raw.decode("utf-8")
    return tuple(f for f in findings_in_text(rel, text) if f.rule == "consumer-mailbox")


@pytest.mark.parametrize("rel", sorted(UPSTREAM_LICENSE_MAILBOX_BLOBS))
def test_published_blob_sha256_matches_the_reviewed_pin(rel: str) -> None:
    raw = _published_blob(rel)
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == UPSTREAM_LICENSE_MAILBOX_BLOBS[rel]
    mirror_digests = {entry.sha256 for entry in REVIEWED_LICENSE_TEXTS if entry.text_relative == rel}
    assert mirror_digests == {digest}


def test_reviewed_license_mailboxes_are_counted_exempt_on_the_published_tree() -> None:
    result = audit_index(REPO_ROOT)
    for rel, lines in _EXPECTED_MAILBOX_LINES.items():
        hits = _mailbox_hits(rel, _published_blob(rel))
        assert tuple(f.line for f in hits) == lines
        exempt = tuple(f for f in result.exempt if f.path == rel)
        findings = tuple(f for f in result.findings if f.path == rel)
        assert findings == ()
        assert tuple((f.line, f.rule) for f in exempt) == tuple((f.line, "consumer-mailbox") for f in hits)
        assert len(exempt) == len(lines)
    assert "exempt=" in result.summary()
    assert len(result.exempt) >= sum(len(lines) for lines in _EXPECTED_MAILBOX_LINES.values())


@pytest.mark.parametrize("rel", sorted(UPSTREAM_LICENSE_MAILBOX_BLOBS))
def test_byte_identical_published_blob_is_exempt_in_a_disposable_index(
    tmp_path: Path, rel: str
) -> None:
    raw = _published_blob(rel)
    root = _repo(tmp_path)
    _stage(root, rel, raw)
    result = audit_index(root)
    hits = _mailbox_hits(rel, raw)
    assert tuple(f.line for f in result.exempt if f.path == rel) == tuple(f.line for f in hits)
    assert tuple(f for f in result.findings if f.path == rel) == ()
    assert f"exempt={len(hits)}" in result.summary()


@pytest.mark.parametrize("rel", sorted(UPSTREAM_LICENSE_MAILBOX_BLOBS))
def test_altered_published_bytes_fail_closed(tmp_path: Path, rel: str) -> None:
    raw = _published_blob(rel) + b"\n"
    root = _repo(tmp_path)
    _stage(root, rel, raw)
    result = audit_index(root)
    hits = _mailbox_hits(rel, raw)
    assert hits
    assert tuple(f for f in result.exempt if f.path == rel) == ()
    assert tuple(f.line for f in result.findings if f.path == rel) == tuple(f.line for f in hits)


@pytest.mark.parametrize("rel", sorted(UPSTREAM_LICENSE_MAILBOX_BLOBS))
def test_same_bytes_at_an_unlisted_path_fail_closed(tmp_path: Path, rel: str) -> None:
    raw = _published_blob(rel)
    other = "docs/legal/unlisted-" + Path(rel).name
    assert other not in UPSTREAM_LICENSE_MAILBOX_BLOBS
    root = _repo(tmp_path)
    _stage(root, other, raw)
    result = audit_index(root)
    hits = _mailbox_hits(other, raw)
    assert hits
    assert result.exempt == ()
    assert tuple(f.line for f in result.findings if f.path == other) == tuple(f.line for f in hits)


@pytest.mark.parametrize("rel", sorted(UPSTREAM_LICENSE_MAILBOX_BLOBS))
def test_private_identifiers_in_a_disposable_copy_fail_closed(tmp_path: Path, rel: str) -> None:
    raw = (
        _published_blob(rel)
        + b"\n"
        + _REAL_HOME.encode("utf-8")
        + b"\n"
        + _CGNAT.encode("utf-8")
        + b"\n"
        + _MAILBOX.encode("utf-8")
        + b"\n"
    )
    root = _repo(tmp_path)
    _stage(root, rel, raw)
    result = audit_index(root)
    assert tuple(f for f in result.exempt if f.path == rel) == ()
    rules = {f.rule for f in result.findings if f.path == rel}
    assert "consumer-mailbox" in rules
    assert "home-path" in rules
    assert "cgnat-address" in rules
    assert _MAILBOX in {f.match for f in result.findings if f.path == rel}


@pytest.mark.parametrize("rel", sorted(UPSTREAM_LICENSE_MAILBOX_BLOBS))
def test_working_tree_bytes_are_not_the_pin(tmp_path: Path, rel: str) -> None:
    raw = _published_blob(rel)
    root = _repo(tmp_path)
    _stage(root, rel, raw)
    (root / rel).write_bytes(raw + b"\n" + _REAL_HOME.encode("utf-8") + b"\n")
    result = audit_index(root)
    hits = _mailbox_hits(rel, raw)
    assert tuple(f.line for f in result.exempt if f.path == rel) == tuple(f.line for f in hits)
    assert "home-path" not in {f.rule for f in result.findings}


@pytest.mark.parametrize("rel", sorted(UPSTREAM_LICENSE_MAILBOX_BLOBS))
def test_checksum_exemption_is_mailbox_only_even_when_the_pin_matches(rel: str) -> None:
    digest = UPSTREAM_LICENSE_MAILBOX_BLOBS[rel]
    assert is_exempt(Finding(rel, 1, "consumer-mailbox", _MAILBOX), digest)
    assert not is_exempt(Finding(rel, 1, "home-path", _REAL_HOME), digest)
    assert not is_exempt(Finding(rel, 1, "cgnat-address", _CGNAT), digest)
    assert not is_exempt(Finding(rel, 1, "consumer-mailbox", _MAILBOX), "")
    assert not is_exempt(Finding(rel, 1, "consumer-mailbox", _MAILBOX), "0" * 64)
    assert rel not in MAILBOX_EXEMPT_PATHS
    assert rel not in GENERATED_TEST_ID_PATHS
