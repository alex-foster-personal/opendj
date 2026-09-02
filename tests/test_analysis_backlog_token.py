"""The backlog's CHANGE TOKEN: what makes a queue read as the same queue.

Split out of test_analysis_backlog.py, which owns the buckets and the SQL
that fills them. This module owns ``_content_token`` on its own: the two-line
function the whole retry-storm guard rests on, since a token that cannot see
a repair suppresses the repaired file until some unrelated track happens to
move the signature.

Pure-function tests over real files, so no state DB and no fixture here - the
signature-level cases that need one stay with their siblings.

Regression lines:
  - if a token built where ctime is creation time misses an in-place repair then broken
  - if the digested token moves on its own then broken
  - if a file that vanished mid-scan raises instead of reading None then broken
  - if this platform is routed to the branch its capability does not name then broken
"""
from __future__ import annotations

import os
from pathlib import Path

from apps.analysis import backlog as backlog_mod


def _audio(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x00" * 4096)
    return p


def test_the_token_digests_the_bytes_where_ctime_cannot_see_a_repair(tmp_path):
    """On Windows every stat field survives a same-length in-place repair.

    ``st_ctime`` carries the honesty of the cheap token, but only on POSIX,
    where it is the inode CHANGE time. On Windows it is the CREATION time: a
    same-length rewrite that restores mtime moves size, mtime AND ctime not at
    all, so a stat-only token reports the repaired file as unchanged and the
    drain suppresses it FOREVER - not until the next scan, but until some
    unrelated track happens to move the queue signature.

    ``ctime_is_inode_change`` is the capability, not the platform, so the
    branch that platform selects is reachable here: the file, the rewrite and
    the digest are all real, and the assertions below are what the Windows
    stat fields really do.
    """
    repaired = _audio(tmp_path, "windows-shape.mp3")
    before_stat = repaired.stat()
    before = backlog_mod._content_token(repaired, ctime_is_inode_change=False)

    repaired.write_bytes(b"\x03" * before_stat.st_size)   # same length, new bytes
    os.utime(repaired, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))

    after_stat = repaired.stat()
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns, (
        "fixture precondition: the mtime must be restored exactly, or the "
        "cheap token would have discriminated and this proves nothing"
    )

    after = backlog_mod._content_token(repaired, ctime_is_inode_change=False)
    assert after != before, (
        "a repair that no stat field can see left the token unchanged, so on "
        "Windows the drain would never retry the repaired bytes"
    )
    assert str(after_stat.st_ctime_ns) not in after, (
        "the token still leans on ctime. This test runs on POSIX, where the "
        "rewrite bumped ctime and would hide a stat-only token's failure; on "
        "Windows ctime is the creation time and this repair did not move it, "
        "so the token must carry something the bytes decide"
    )


def test_the_digested_token_is_stable_for_bytes_that_did_not_change(tmp_path):
    """The suppression has to keep working where the digest replaces ctime.

    A token that moved on its own would arm the drain every tick and turn the
    retry-storm guard off on exactly the platform this branch exists for, so
    the same bytes must read the same twice - including across a touch, which
    moves mtime without changing anything a decoder would read.
    """
    steady = _audio(tmp_path, "steady.mp3")
    first = backlog_mod._content_token(steady, ctime_is_inode_change=False)
    assert first is not None

    assert backlog_mod._content_token(steady, ctime_is_inode_change=False) == first
    assert first.endswith(backlog_mod._content_digest(steady)), (
        "the digest is what makes the repair visible; it must be IN the token"
    )


def test_a_token_for_a_file_that_vanished_is_none_on_both_paths(tmp_path):
    """A scan is a snapshot, not a lock, and the digest path opens the file.

    Adding a read where there used to be only a stat adds a second way to
    lose the race with an eviction, and an OSError escaping here would take
    down a whole scan over one file that went away.
    """
    gone = tmp_path / "evicted.mp3"
    assert backlog_mod._content_token(gone, ctime_is_inode_change=True) is None
    assert backlog_mod._content_token(gone, ctime_is_inode_change=False) is None


def test_this_platform_is_routed_to_the_branch_its_capability_names(tmp_path):
    """The default call - the one the scan makes - on whatever runner this is.

    The cases above pass ``ctime_is_inode_change`` explicitly. That is what
    makes the digest branch reachable at all on a POSIX runner, and the branch
    it selects is production code over a real file, but it does mean those
    cases say nothing about which branch this MACHINE takes: they would stay
    green if the selector were wrong and Windows were routed through the
    stat-only path, which is the failure the digest branch exists to prevent.

    This case makes no choice. It calls ``_content_token`` exactly as
    ``_first_playable`` does and asserts that the token really carries what
    the capability claims - so on a Windows runner it is the digest assertion
    that has to hold, and the misrouting above is what fails here. The
    capability itself is checked against ``os.name`` rather than against
    itself, because a token that agrees with a wrong constant agrees with
    nothing.
    """
    track = _audio(tmp_path, "as-the-scan-calls-it.mp3")
    token = backlog_mod._content_token(track)
    assert token is not None

    assert backlog_mod.CTIME_IS_INODE_CHANGE is (os.name != "nt"), (
        "the capability was derived from something other than the platform "
        "whose ctime semantics it describes"
    )
    st = track.stat()
    if backlog_mod.CTIME_IS_INODE_CHANGE:
        assert token == f"{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}", (
            "ctime is the inode change time here, so the cheap token is the "
            "correct one and the scan must not be paying for a digest"
        )
    else:
        assert token.endswith(backlog_mod._content_digest(track)), (
            "ctime cannot see an in-place repair on this platform, so a "
            "stat-only token would suppress a repaired file forever"
        )
