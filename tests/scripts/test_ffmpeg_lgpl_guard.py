"""The payload's ffmpeg is LGPL, carries libsoxr, and the payload finds it.

Regression one-liners:
  - if a buildconf containing --enable-gpl passes the guard then broken
  - if a buildconf containing --enable-nonfree passes the guard then broken
  - if a buildconf without --enable-libsoxr passes the guard then broken
  - if an ffmpeg whose -L reports the GPL passes the guard then broken
  - if the guard fires on the real pinned configure line then broken
  - if the payload launcher stops exporting ODJ_FFMPEG_BIN then broken
  - if the configure line ever asks for --enable-gpl or --enable-nonfree then broken
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from scripts import build_ffmpeg_lgpl as lgpl
from scripts.build_engine_payload import ENGINE_LAUNCHER_TEMPLATE, FFMPEG_RELATIVE

LGPL_L = (
    "ffmpeg is free software; you can redistribute it and/or\n"
    "modify it under the terms of the GNU Lesser General Public\n"
    "License as published by the Free Software Foundation; either\n"
)
GPL_L = (
    "ffmpeg is free software; you can redistribute it and/or modify\n"
    "it under the terms of the GNU General Public License as published by\n"
)


def _buildconf(*extra: str) -> str:
    return "  configuration:\n" + "\n".join(f"    {flag}" for flag in [
        "--prefix=/tmp/x", "--disable-everything", "--enable-libsoxr", *extra,
    ]) + "\n"


def _fake_ffmpeg(tmp_path: Path, buildconf: str, licence: str) -> Path:
    """An executable that answers -buildconf / -L / -version like ffmpeg does."""
    (tmp_path / "buildconf.txt").write_text(buildconf)
    (tmp_path / "licence.txt").write_text(licence)
    script = tmp_path / "ffmpeg"
    script.write_text(
        "#!/bin/sh\n"
        f'case "$*" in *-buildconf*) cat "{tmp_path}/buildconf.txt";;\n'
        f'  *-L*) cat "{tmp_path}/licence.txt";;\n'
        '  *-version*) echo "ffmpeg version fake";;\n'
        "  *) printf '\\001\\002';;\nesac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_clean_lgpl_buildconf_passes() -> None:
    """[if] libsoxr on, no gpl/nonfree [then] no violations."""
    assert lgpl.buildconf_violations(_buildconf()) == []


@pytest.mark.parametrize("flag", ["--enable-gpl", "--enable-nonfree"])
def test_forbidden_flag_is_a_violation(flag: str) -> None:
    """[if] the buildconf carries a forbidden flag [then] the guard names it."""
    assert lgpl.buildconf_violations(_buildconf(flag)) == [f"buildconf contains {flag}"]


def test_missing_libsoxr_is_a_violation() -> None:
    """[if] the build lacks libsoxr [then] the fingerprint cannot run, so refuse."""
    conf = _buildconf().replace("--enable-libsoxr", "")
    assert lgpl.buildconf_violations(conf) == ["buildconf lacks --enable-libsoxr"]


def test_gpl_licence_text_is_a_violation() -> None:
    """[if] ffmpeg -L reports the GPL [then] refuse."""
    assert lgpl.licence_violations(GPL_L) != []
    assert lgpl.licence_violations(LGPL_L) == []


def test_mutation_control_fake_gpl_binary_is_refused(tmp_path: Path) -> None:
    """[if] a real-shaped binary reports --enable-gpl [then] verify_lgpl_binary raises.

    Mutation control for the whole guard path (subprocess + parse + raise):
    the same fake with a clean buildconf must pass, so the refusal is caused
    by the flag and nothing else.
    """
    clean = tmp_path / "clean"
    clean.mkdir()
    assert lgpl.verify_lgpl_binary(_fake_ffmpeg(clean, _buildconf(), LGPL_L))["version"]
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    with pytest.raises(lgpl.FfmpegBuildError, match="--enable-gpl"):
        lgpl.verify_lgpl_binary(_fake_ffmpeg(dirty, _buildconf("--enable-gpl"), LGPL_L))


def test_pinned_configure_line_is_lgpl_with_soxr() -> None:
    """[if] the pinned configure line asks for gpl/nonfree or drops soxr [then] broken."""
    line = " ".join(lgpl.configure_args(Path("/tmp/prefix")))
    assert lgpl.buildconf_violations(line) == []


def test_launcher_exports_the_bundled_ffmpeg() -> None:
    """[if] the launcher stops exporting ODJ_FFMPEG_BIN [then] the app decodes with PATH's ffmpeg."""
    assert f'ODJ_FFMPEG_BIN="$payload/{FFMPEG_RELATIVE}"' in ENGINE_LAUNCHER_TEMPLATE
    assert "export ODJ_FFMPEG_BIN" in ENGINE_LAUNCHER_TEMPLATE


def test_notice_carries_the_offer_and_the_configure_line() -> None:
    """[if] NOTICE-ffmpeg loses the configure line, a source sha or the offer [then] broken."""
    args = lgpl.configure_args(Path("/tmp/prefix"))
    notice = lgpl.notice_text(args)
    assert " ".join(args) in notice
    assert lgpl.FFMPEG.sha256 in notice and lgpl.SOXR.sha256 in notice
    assert "Written offer" in notice
