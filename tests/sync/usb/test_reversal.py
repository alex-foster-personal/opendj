"""Tests for apps.sync.usb.reversal."""
from __future__ import annotations

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
    # bash -n validates the generated script.
    ok, err = verify_syntax(log.path)
    assert ok, err


@pytest.mark.requirement("CAT-02")
def test_verify_syntax_rejects_garbage(tmp_path) -> None:
    bad = tmp_path / "bad.sh"
    bad.write_text("#!/bin/bash\nif then fi\n", encoding="utf-8")
    ok, _err = verify_syntax(bad)
    assert not ok
