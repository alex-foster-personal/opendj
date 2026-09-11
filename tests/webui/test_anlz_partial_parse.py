"""H10: one unparseable ANLZ file must not take a whole track's analysis down.

rekordbox writes files pyrekordbox cannot read. The 100 acapellas imported
Sat 8 Aug 2026 have an `ANLZ0000.EXT` whose colour-waveform tag fails a
construct const check while the `.DAT` (PQTZ beatgrid, PWAV, PCOB cues) and
`.2EX` (PWV6/PWV7 tri-band) parse perfectly. `read_anlz_files` parses the set
in one call, so a single bad sibling used to 500 the whole track.

Losing one file is not the same as losing the analysis: the parseable files are
kept, and the failure is RETURNED rather than swallowed so the client can name
which lane is missing instead of painting an empty waveform that looks like
real silence. `tests/test_rb_assets.py` only ever asserted the happy path
(`unreadable_anlz == []`), which is precisely the assertion a regression keeps
passing.

Real rekordbox ANLZ files from `tests/fixtures/rb-usb-export`, corrupted by
truncation/byte-flipping in a tmp copy. Nothing here is synthesised.

Acceptance criteria (UNCAPTURED-REQUIREMENTS.md H10):
  [if] a track dir has one corrupt .EXT and a healthy .DAT [then] /anlz returns
       beatgrid + cues and lists the .EXT in unreadable_anlz ⛔️ a 500, or a 200
       with empty bands and empty unreadable_anlz
  [if] every ANLZ file is corrupt [then] the endpoint raises ANALYSIS_NOT_FOUND
       naming the files ⛔️ a 200 with empty bands the UI paints as silence
  [if] a lane is missing because its file was unreadable [then] the client can
       name which lane ⛔️ a blank waveform row indistinguishable from a
       genuinely silent track

-Claude
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi import HTTPException

from apps.webui.server import rb_vendor
from tests.fixtures.conftest import resolve_required_fixture


def _fixture_dir() -> Path:
    """Resolve the rb-usb-export analysis dir through the resolver.

    Routes through ``resolve_required_fixture()`` (rather than a hard-coded
    repo path) so this GUARD-01 acceptance test keeps finding the fixture,
    and keeps refusing to silently skip (fails closed unless
    MDT_ALLOW_MISSING_FIXTURES=1), once the in-repo directory leaves and
    only ``rb-usb-export.extern`` remains (PR #718).
    """
    root = resolve_required_fixture("rb-usb-export")
    return root / "PIONEER" / "USBANLZ" / "P063" / "00016827"


def _real_dir(tmp_path: Path) -> Path:
    """A copy of a real rekordbox analysis directory (.DAT, .EXT, .2EX)."""
    dst = tmp_path / "anlz"
    shutil.copytree(_fixture_dir(), dst)
    return dst


def _corrupt(path: Path) -> None:
    """Break the file the way rekordbox's own writer does: valid header, bad body."""
    raw = bytearray(path.read_bytes())
    for i in range(len(raw) // 4, min(len(raw), len(raw) // 4 + 512)):
        raw[i] ^= 0xFF
    path.write_bytes(bytes(raw[: len(raw) // 2]))


@pytest.mark.requirement("GUARD-01")
def test_fixture_directory_parses_cleanly_before_anything_is_broken(
    tmp_path: Path,
) -> None:
    """Baseline: without this the degraded cases prove nothing."""
    tags, unreadable = rb_vendor._first_tags(_real_dir(tmp_path))
    assert unreadable == []
    assert "PQTZ" in tags and "PWV6" in tags and "PWV7" in tags


@pytest.mark.requirement("GUARD-01")
def test_corrupt_ext_still_serves_the_dat_and_2ex_lanes(tmp_path: Path) -> None:
    directory = _real_dir(tmp_path)
    _corrupt(directory / "ANLZ0000.EXT")

    tags, unreadable = rb_vendor._first_tags(directory)

    assert unreadable == ["ANLZ0000.EXT"], (
        f"the unreadable file was swallowed or misnamed: {unreadable}"
    )
    assert "PQTZ" in tags, "the .DAT beatgrid was lost with its unrelated sibling"
    assert "PWV6" in tags and "PWV7" in tags, "the .2EX tri-band lanes were lost"
    beatgrid, _times = rb_vendor._beatgrid_payload(tags)
    assert beatgrid["beat_count"] > 0, (
        "a corrupt .EXT emptied the beatgrid the .DAT parsed perfectly well"
    )


@pytest.mark.requirement("GUARD-01")
def test_corrupt_2ex_still_serves_the_dat_lanes(tmp_path: Path) -> None:
    """The other direction: the tri-band carrier is the one that dies."""
    directory = _real_dir(tmp_path)
    _corrupt(directory / "ANLZ0000.2EX")

    tags, unreadable = rb_vendor._first_tags(directory)

    assert unreadable == ["ANLZ0000.2EX"]
    assert "PQTZ" in tags and "PWAV" in tags
    assert "PWV6" not in tags and "PWV7" not in tags


@pytest.mark.requirement("GUARD-01")
def test_the_unreadable_list_names_the_extension_so_a_lane_can_be_identified(
    tmp_path: Path,
) -> None:
    """A client must be able to say WHICH lane is absent, not just 'something'.

    The tri-band preview/detail lanes come from the .2EX; naming the file by
    extension is what lets the UI label an empty row 'unreadable' rather than
    'silent'.
    """
    directory = _real_dir(tmp_path)
    _corrupt(directory / "ANLZ0000.2EX")
    _corrupt(directory / "ANLZ0000.EXT")

    tags, unreadable = rb_vendor._first_tags(directory)

    assert sorted(unreadable) == ["ANLZ0000.2EX", "ANLZ0000.EXT"]
    assert {Path(n).suffix.upper() for n in unreadable} == {".2EX", ".EXT"}
    # The surviving .DAT still answers, so this is a degraded track, not a dead one.
    assert "PQTZ" in tags


@pytest.mark.requirement("GUARD-01")
def test_every_file_corrupt_raises_analysis_not_found_naming_the_files(
    tmp_path: Path,
) -> None:
    directory = _real_dir(tmp_path)
    for name in ("ANLZ0000.DAT", "ANLZ0000.EXT", "ANLZ0000.2EX"):
        _corrupt(directory / name)

    with pytest.raises(HTTPException) as exc:
        rb_vendor._first_tags(directory)

    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "ANALYSIS_NOT_FOUND"
    message = exc.value.detail["message"]
    for name in ("ANLZ0000.DAT", "ANLZ0000.EXT", "ANLZ0000.2EX"):
        assert name in message, f"{name} not named in the 404: {message}"


@pytest.mark.requirement("GUARD-01")
def test_an_empty_directory_raises_rather_than_returning_empty_bands(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(HTTPException) as exc:
        rb_vendor._first_tags(empty)
    assert exc.value.detail["code"] == "ANALYSIS_NOT_FOUND"


@pytest.mark.requirement("GUARD-01")
def test_non_anlz_files_in_the_directory_are_ignored_not_reported_unreadable(
    tmp_path: Path,
) -> None:
    directory = _real_dir(tmp_path)
    (directory / "notes.txt").write_text("scratch", encoding="utf-8")
    _tags, unreadable = rb_vendor._first_tags(directory)
    assert unreadable == []

pytestmark = pytest.mark.rb_parity
