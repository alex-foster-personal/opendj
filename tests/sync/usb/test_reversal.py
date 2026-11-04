"""Tests for apps.sync.usb.reversal."""
from __future__ import annotations

import shlex
from pathlib import Path, PurePosixPath

import pytest

from apps.sync.usb.copy import CopyResult
from apps.sync.usb.diff import Op
from apps.sync.usb.reversal import ReversalLog, verify_syntax


def _op(kind: str, dst: Path, src: Path | None = None) -> Op:
    return Op(
        kind=kind,
        dst=dst,
        dst_rel=PurePosixPath(dst.name),
        src=src,
        stable_id="sid",
        expected_hash="h",
        reason="r",
        bytes_estimate=1,
    )


@pytest.mark.requirement("CAT-02")
def test_reversal_records_copy_delete_rename(tmp_path) -> None:
    """[if] ops are appended [then] the script carries each inverse command, [else stop]."""
    log = ReversalLog.open(
        dir_=tmp_path,
        profile_name="p",
        mode="cautious",
        reason="test",
        drive_uuid="UUID",
        tag="apply",
    )
    dst = tmp_path / "a.mp3"
    dst.write_bytes(b"x")
    # copy
    log.append(CopyResult(op=_op("copy", dst), ok=True, actual_hash="", dst_size=1))
    # overwrite with backup
    backup = tmp_path / "a.mp3.bak-20260101-000000"
    backup.write_bytes(b"old")
    log.append(
        CopyResult(
            op=_op("overwrite", dst),
            ok=True,
            actual_hash="",
            dst_size=1,
            backup_path=backup,
        )
    )
    # overwrite without backup
    log.append(CopyResult(op=_op("overwrite", dst), ok=True, actual_hash="", dst_size=1))
    # delete
    log.append(
        CopyResult(op=_op("delete", dst), ok=True, actual_hash="", dst_size=0)
    )
    # rename: must emit `mv dst -> backup_path` inverse. Regression for [C1].
    rename_new = tmp_path / "new" / "a.mp3"
    rename_old = tmp_path / "old" / "a.mp3"
    log.append(
        CopyResult(
            op=_op("rename", rename_new, src=rename_old),
            ok=True,
            actual_hash="",
            dst_size=1,
            backup_path=rename_old,
        )
    )
    # failed op emits comment only
    log.append(
        CopyResult(
            op=_op("copy", dst),
            ok=False,
            actual_hash="",
            dst_size=0,
            error="boom",
        )
    )
    log.close(summary="done")

    text = log.path.read_text()
    assert "set -euo pipefail" in text
    assert "profile:    p" in text
    assert "drive_uuid: UUID" in text
    # Regression for [C1]: rename op must produce an inverse `mv` line.
    # Match through shlex.quote: POSIX tmp paths stay bare, Windows
    # backslash paths are quoted, and the script must carry exactly that.
    expected_mv = f"mv -v -- {shlex.quote(str(rename_new))} {shlex.quote(str(rename_old))}"
    assert expected_mv in text
    # bash -n validates the generated script.
    ok, err = verify_syntax(log.path)
    assert ok, err


@pytest.mark.requirement("CAT-02")
def test_verify_syntax_rejects_garbage(tmp_path) -> None:
    """[if] the script has a bash syntax error [then] bash itself rejects it, [else stop]."""
    bad = tmp_path / "bad.sh"
    bad.write_text("#!/bin/bash\nif then fi\n", encoding="utf-8")
    ok, err = verify_syntax(bad)
    assert not ok
    # Only bash's own diagnostic proves a real check ran: "bash not available"
    # and "timeout" also return not-ok without parsing anything.
    assert "syntax error" in err, err


@pytest.mark.requirement("CAT-02")
def test_verify_syntax_accepts_valid_script(tmp_path) -> None:
    """[if] a valid reversal script is checked [then] ok with empty stderr, [else stop]."""
    # Regression: on Windows a bare ``bash`` resolved to the System32 WSL
    # launcher, which returned rc=1 with empty stderr for VALID scripts, so
    # every reversal log was reported as a syntax failure.
    good = tmp_path / "good.sh"
    good.write_text("#!/usr/bin/env bash\nset -euo pipefail\necho ok\n", encoding="utf-8")
    ok, err = verify_syntax(good)
    assert ok, err
    assert err == ""
