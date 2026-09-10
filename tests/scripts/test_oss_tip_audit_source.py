"""oss_tip_audit: WHERE the gate gets its bytes, as distinct from what it matches.

Split out of test_oss_tip_audit.py at the 600-line file limit. The seam is real:
the sibling modules ask which strings a rule matches, this one asks whether the
gate is reading the thing that would actually be published.

Regression lines:
- if the gate reads working-tree bytes instead of the indexed blob then broken
- if the gate reads only ONE of HEAD and the index then broken (they diverge both ways)
- if a submodule pathname is dropped along with its absent blob then broken
- if a symlink is audited by its target's bytes or its target's contents rather
  than by its link text then broken
- if a write-once record's CONTENT reaches audit_index then broken
- if a record's PATHNAME stops being audited then broken
- if the filesystem variant stops reading the filesystem then broken
- if an unreadable committed tree reports clean instead of raising then broken
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.oss_tip_audit import RECORD_PATHS, audit_index, audit_paths

# Assembled from fragments: the gate scans its own test suite.
_USERS = "/" + "Users" + "/"
_LOGIN = "jdoe"
_REAL_HOME_ROOT = _USERS + _LOGIN


def test_the_gate_reads_the_indexed_blob_not_the_working_tree(tmp_path: Path) -> None:
    """Codex P1, and the most consequential finding on this PR.

    `git ls-files` enumerates the INDEX. Pairing those pathnames with filesystem
    bytes audits two different things at once, so a file scrubbed or deleted
    only in the working tree reported clean while the blob that would actually
    be published still carried the identifier. A publication gate certifying
    content it never read is the exact failure this module exists to prevent.

    Both halves of the reported scenario are asserted, plus deletion, which is
    harsher than what was reported.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    secret = _USERS + _LOGIN + "/Music"
    leak = tmp_path / "leak.md"
    leak.write_text(f"path {secret}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "leak.md"], check=True)

    assert [f.match for f in audit_index(tmp_path).findings] == [_REAL_HOME_ROOT]

    leak.write_text("all clean now\n", encoding="utf-8")
    assert [f.match for f in audit_index(tmp_path).findings] == [_REAL_HOME_ROOT], (
        "scrubbing only the working tree must not clear the gate"
    )

    leak.unlink()
    assert [f.match for f in audit_index(tmp_path).findings] == [_REAL_HOME_ROOT], (
        "deleting only from the working tree must not clear the gate"
    )

    # Control: the filesystem variant is SUPPOSED to be blind here. Without it
    # the assertions above could pass for a version that never reads a tree at
    # all, and audit_paths is what the rest of this suite exercises.
    assert audit_paths(tmp_path, [leak]).findings == ()


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(tmp_path), "config", key, value], check=True)
    return tmp_path


def test_a_leak_in_head_survives_a_scrub_that_is_only_staged(tmp_path: Path) -> None:
    """Codex P1, the mirror image of the working-tree report.

    Scrubbing in the INDEX leaves the leaking commit in HEAD, and HEAD is what a
    visibility flip publishes right now. An index-only gate reported clean on it.
    """
    root = _repo(tmp_path)
    leak = root / "leak.md"
    leak.write_text(f"{_REAL_HOME_ROOT}/Music\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "leak.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "leak"], check=True)
    assert [f.match for f in audit_index(root).findings] == [_REAL_HOME_ROOT]

    leak.write_text("clean now\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "leak.md"], check=True)
    assert [f.match for f in audit_index(root).findings] == [_REAL_HOME_ROOT], (
        "scrubbing only the index must not clear a leak that is still in HEAD"
    )


def test_a_leak_staged_over_a_clean_head_is_also_caught(tmp_path: Path) -> None:
    """The other direction, so the fix cannot be "read HEAD instead".

    The index is what HEAD becomes on the next commit, and this gate runs before
    that commit. Reading either source alone leaves one of these two blind.
    """
    root = _repo(tmp_path)
    path = root / "ok.md"
    path.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "ok.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "ok"], check=True)

    path.write_text(f"{_REAL_HOME_ROOT}/Music\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "ok.md"], check=True)
    assert [f.match for f in audit_index(root).findings] == [_REAL_HOME_ROOT]


def test_a_submodule_pathname_is_audited_even_though_it_has_no_blob(
    tmp_path: Path,
) -> None:
    """Codex P1: a gitlink has no blob to read, but git publishes its NAME, and
    dropping the entry dropped the name with it."""
    root = _repo(tmp_path)
    inner = tmp_path.parent / "inner-repo"
    inner.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(inner)], check=True)
    for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(inner), "config", key, value], check=True)
    (inner / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(inner), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(inner), "commit", "-qm", "init"], check=True)

    named = "person" + "@" + "private-domain" + ".com"
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(inner),
            named,
        ],
        check=True,
        capture_output=True,
    )
    findings = audit_index(root).findings
    # Attributed to the GITLINK ENTRY at line 0, not merely present somewhere.
    # `git submodule add` also writes `.gitmodules`, which contains the path as
    # ordinary text, so a looser assertion passes with the gitlink dropped
    # entirely -- it did, when this guard was mutated away. The control is that
    # the finding must carry the submodule's own path.
    gitlink = [(f.path, f.line, f.match) for f in findings if f.path == named]
    assert gitlink == [(named, 0, named)], [f.render() for f in findings]


def test_a_symlink_is_audited_by_its_link_text_not_by_its_target(tmp_path: Path) -> None:
    """Codex P1, #1440. Git publishes the LINK TEXT of a symlink.

    `is_file()` follows the link instead, and both directions were wrong: a
    BROKEN link is not a file at all, so it vanished from the scan, and a
    WORKING one had the target's contents audited under the link's pathname --
    a string git never publishes. A link's own text is the only thing this gate
    has any business reading.
    """
    mailbox = "someone" + "@" + "private-domain" + ".com"
    target = tmp_path / "target.md"
    target.write_text(f"{mailbox}\n", encoding="utf-8")
    broken = tmp_path / "broken.link"
    working = tmp_path / "working.link"
    os.symlink(_REAL_HOME_ROOT + "/gone.md", broken)
    os.symlink(target, working)

    result = audit_paths(tmp_path, [broken, working])

    # The broken link's TEXT names a home, and it is reported at line 1 of the
    # text, not skipped for pointing at nothing.
    assert [(f.path, f.line, f.match) for f in result.findings] == [
        ("broken.link", 1, _REAL_HOME_ROOT)
    ], [f.render() for f in result.findings]
    # The working link's TARGET is the leak and its link text is not, so reading
    # the target would report a mailbox against a pathname git does not publish.
    assert not [f for f in result.findings if f.path == "working.link"], [
        f.render() for f in result.findings
    ]
    # Control: the target IS a finding when it is audited as its own path, so
    # the silence above is about the link and not about the planted address.
    assert [f.match for f in audit_paths(tmp_path, [target]).findings] == [mailbox]


def test_a_pathname_git_permits_but_utf8_forbids_is_audited_not_a_crash(
    tmp_path: Path,
) -> None:
    """Codex P2, #1440, discussion_r3972967010.

    Git stores pathnames as BYTES and permits ones that are not valid UTF-8.
    The parse used a plain `.decode()`, so a single such path raised
    UnicodeDecodeError before any blob was read. On a MANDATORY publication gate
    that is worse than a false finding: the gate dies and blocks every unrelated
    change, and it dies for a reason no output explains.

    The assertion is that the leak in that file is REPORTED, not merely that the
    call returns. A version that swallowed the path silently would also not
    crash, and would certify an unread blob as clean.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    # A lone 0xff byte is valid in a git pathname and cannot begin a UTF-8
    # sequence. The INDEX is written directly rather than through a file on
    # disk, because APFS and HFS+ reject a non-UTF-8 filename outright, so the
    # on-disk route cannot express the case this gate has to survive. The index
    # is also the thing the gate actually reads, which makes this the more
    # faithful setup rather than a workaround.
    blob = subprocess.run(
        ["git", "-C", str(tmp_path), "hash-object", "-w", "--stdin"],
        input=f"path {_USERS + _LOGIN}/Music\n".encode(),
        capture_output=True,
        check=True,
    ).stdout.decode().strip()
    subprocess.run(
        [
            b"git",
            b"-C",
            str(tmp_path).encode(),
            b"update-index",
            b"--add",
            b"--cacheinfo",
            b"100644," + blob.encode() + b",le\xffak.md",
        ],
        check=True,
    )

    result = audit_index(tmp_path)

    assert [f.match for f in result.findings] == [_REAL_HOME_ROOT], (
        f"the undecodable pathname was not audited: {[f.render() for f in result.findings]}"
    )
    # Rendering must survive it too, or the crash simply moves one line later
    # to the print. Both halves are the same defect.
    #
    # ENCODED, not merely produced: an f-string interpolates a surrogate-escaped
    # str without complaint, so `assert render()` passes for the unfixed version
    # and proves nothing. `print` is what encodes, and encoding is what raises.
    # Mutating the fix out left this test green until it was written this way.
    result.findings[0].render().encode("utf-8")
    assert result.scanned >= 1


def test_an_unreadable_head_is_an_audit_failure_not_a_clean_report(
    tmp_path: Path,
) -> None:
    """Codex P1 BLOCKING, #1440, discussion_r3975241642.

    `git ls-tree HEAD` was allowed to fail for ANY reason and the audit simply
    dropped HEAD. That folds "this repository has no commit yet", which is a
    legitimate state, together with "the committed tree could not be read",
    which is a failed measurement. With the scrub staged in the index, the
    second case reported `findings=0` for a commit nobody had inspected, and
    zero findings is exactly what a clean tree reports too.

    Set up so the index is CLEAN and HEAD is the only thing carrying the leak,
    because that is the arrangement in which the bug is invisible: an index
    that also leaked would have produced a finding for the wrong reason.
    """
    root = _repo(tmp_path)
    leak = root / "leak.md"
    leak.write_text(f"{_REAL_HOME_ROOT}/Music\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "leak.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "leak"], check=True)
    leak.write_text("clean now\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "leak.md"], check=True)
    # Positive control: while HEAD is readable the leak is reported, so a later
    # silence is attributable to the broken tree and not to a missing leak.
    assert [f.match for f in audit_index(root).findings] == [_REAL_HOME_ROOT]

    # Break the committed tree while leaving every REF intact: the branch still
    # exists and HEAD still points at it, so nothing about this repository is
    # unborn. Only the object needed to enumerate the commit is gone.
    tree_sha = (
        subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    (root / ".git" / "objects" / tree_sha[:2] / tree_sha[2:]).unlink()

    with pytest.raises(OSError) as raised:
        audit_index(root)
    assert "ls-tree" in str(raised.value), (
        "the failure must name what could not be read, not just fail"
    )


def test_an_unborn_repository_still_audits_its_index(tmp_path: Path) -> None:
    """The other direction, and the reason the fix above is not `check=True`.

    A repository with no commit yet has nothing to publish from HEAD, and that
    is not a failed measurement. Over-correcting to "any ls-tree failure is
    fatal" would make the gate unrunnable on a fresh `git init`, which is a
    real path: this suite's own fixtures stage before they commit.
    """
    root = _repo(tmp_path)
    leak = root / "leak.md"
    leak.write_text(f"{_REAL_HOME_ROOT}/Music\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "leak.md"], check=True)
    assert [f.match for f in audit_index(root).findings] == [_REAL_HOME_ROOT]


# ----- the record scope-out, at the source the walk actually reads ---------------------


def test_a_record_is_scoped_out_of_the_index_and_counted(tmp_path: Path) -> None:
    """The scope-out and its COUNT apply to audit_index too, which is the walk
    CI runs. This is a SOURCE question rather than a spelling one: the gate is
    reading the right bytes and must decline to judge them, and a count that
    silently became a clean report would hide the trade the scope-out makes.
    """
    root = _repo(tmp_path)
    record = root / RECORD_PATHS[0] / "note.md"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(f"ran on {_REAL_HOME_ROOT}/Music\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)

    scoped = audit_index(root)

    assert scoped.findings == (), [f.render() for f in scoped.findings]
    assert scoped.records == 1, scoped.summary()
    assert scoped.scanned == 0, scoped.summary()
    # The escape hatch reads the same bytes and DOES judge them, so the silence
    # above is the scope-out and not an unread blob.
    full = audit_index(root, include_records=True)
    assert [f.rule for f in full.findings] == ["home-path"]
    assert full.records == 0, full.summary()
    assert full.scanned == 1, full.summary()


def test_a_records_pathname_is_audited_by_the_index_walk(tmp_path: Path) -> None:
    """The scope-out covers CONTENT only, at this source as at the other. A
    record whose own NAME carries an address is still reported, so the
    exemption cannot become a smuggling route for anything path-shaped.
    """
    root = _repo(tmp_path)
    mailbox = "someone" + "@" + "private-domain" + ".com"
    record = root / RECORD_PATHS[0] / f"notes-{mailbox}.md"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)

    findings = audit_index(root).findings

    # The match itself carries the trailing `.md`, which is what an address
    # followed by a file extension looks like; the PATHNAME and the rule are
    # what this asserts, because the match string is the capture's business.
    assert [(f.path, f.line, f.rule) for f in findings] == [
        (f"{RECORD_PATHS[0]}notes-{mailbox}.md", 0, "consumer-mailbox")
    ], [f.render() for f in findings]
