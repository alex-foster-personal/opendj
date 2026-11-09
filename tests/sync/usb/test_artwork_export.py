"""USB stick track artwork (USBEXP-10048, issue #10048).

[if] a track has library cover art [then] export writes JPEGs and pdb ids round-trip, [else stop].

Round trip: plan_artwork -> test pdb builder + write_artwork -> read_usb_export
and the stick browser (stick_library). Covers are generated 300x300 PNGs.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest
from PIL import Image

from apps.sync.usb import stick_library as sl
from apps.sync.usb.pioneer.__main__ import main
from apps.sync.usb.pioneer.artwork_export import (
    ArtworkExportError,
    ArtworkPlan,
    ArtworkSource,
    artwork_pdb_path,
    medium_relative_path,
    plan_artwork,
    verify_artwork,
    write_artwork,
)
from apps.sync.usb.pioneer.reader import read_usb_export
from tests.sync.usb.export_pdb_builder import PdbExport, PdbTrack, write_export_pdb

pytestmark = pytest.mark.requirement("USBEXP-10048")

UUID = "0000AAAA-1111-2222-3333-444455556666"


@pytest.fixture(autouse=True)
def _fresh_resolver_state() -> Iterator[None]:
    sl._reset_for_tests()
    yield
    sl._reset_for_tests()


def _png(color: tuple[int, ...], size: tuple[int, int] = (300, 300)) -> bytes:
    mode = "RGBA" if len(color) == 4 else "RGB"
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, "PNG")
    return out.getvalue()


def _source(covers: Mapping[str, bytes]) -> ArtworkSource:
    return covers.get


def _build_stick(root: Path, stable_ids: Sequence[str], plan: ArtworkPlan) -> None:
    tracks = [
        PdbTrack(
            id=n,
            title=sid,
            file_path=f"/Contents/{sid}.flac",
            artwork_id=plan.track_artwork_ids[sid],
        )
        for n, sid in enumerate(stable_ids, start=1)
    ]
    write_export_pdb(root, PdbExport(tracks=tracks, artwork=plan.pdb_table()))
    write_artwork(plan, root)


class _Scan:
    def __init__(self, mount: Path) -> None:
        self.volumes = [sl.MountedVolume(volume_id=f"vol:{UUID}", volume_uuid=UUID, name="STICK", mount_path=mount)]

    def __call__(self) -> Sequence[sl.MountedVolume]:
        return list(self.volumes)


def _artwork_files(root: Path) -> list[Path]:
    return sorted(p for p in (root / "PIONEER" / "Artwork").rglob("*") if p.is_file())


# ----- acceptance ------------------------------------------------------------


def test_track_with_cover_gets_small_and_medium_jpegs_at_served_sizes(tmp_path: Path) -> None:
    root = tmp_path / "STICK"
    plan = plan_artwork(["sid1"], _source({"sid1": _png((200, 30, 30))}))
    _build_stick(root, ["sid1"], plan)

    small = root / "PIONEER/Artwork/00001/a1.jpg"
    medium = root / "PIONEER/Artwork/00001/a1_m.jpg"
    with Image.open(small) as s_img, Image.open(medium) as m_img:
        assert (s_img.format, s_img.size) == ("JPEG", (80, 80)), "if a1.jpg is not an 80px JPEG then broken"
        assert (m_img.format, m_img.size) == ("JPEG", (240, 240)), "if a1_m.jpg is not a 240px JPEG then broken"

    track = read_usb_export(root)["tracks"][0]
    assert track["artwork_present"] is True, "if read_usb_export misses a written cover then broken"

    scan = _Scan(root)
    assert 1 in sl.open_stick_library(UUID, scan).artwork_available, (
        "if the stick browser does not list the track's artwork then broken"
    )
    resolved = sl.resolve_stick_track(sl.mint_stick_track_id(UUID, 1), scan)
    assert sl.stick_artwork_file(resolved, "s") == small, "if stick size s is not a1.jpg then broken"
    assert sl.stick_artwork_file(resolved, "m") == medium, "if stick size m is not a1_m.jpg then broken"


def test_rgba_cover_is_flattened_to_jpeg(tmp_path: Path) -> None:
    plan = plan_artwork(["sid1"], _source({"sid1": _png((10, 200, 10, 128))}))
    with Image.open(io.BytesIO(plan.rows[0].medium.data)) as img:
        assert img.format == "JPEG" and img.mode == "RGB", "if an RGBA cover is not flattened to JPEG then broken"


def test_two_tracks_sharing_a_cover_share_one_row_and_one_pair_of_files(tmp_path: Path) -> None:
    root = tmp_path / "STICK"
    cover = _png((30, 30, 200))
    ids = ["sid1", "sid2"]
    plan = plan_artwork(ids, _source({"sid1": cover, "sid2": cover}))
    _build_stick(root, ids, plan)

    assert len(plan.rows) == 1, "if a shared cover gets two artwork rows then broken"
    assert dict(plan.track_artwork_ids) == {"sid1": 1, "sid2": 1}, (
        "if shared-cover tracks differ in artwork id then broken"
    )
    assert len(plan.files()) == 2, "if a shared cover plans more than one pair of files then broken"
    assert len(_artwork_files(root)) == 2, "if a shared cover writes more than two files then broken"
    assert all(t["artwork_present"] for t in read_usb_export(root)["tracks"])


def test_track_without_cover_gets_id_zero_and_no_file(tmp_path: Path) -> None:
    root = tmp_path / "STICK"
    ids = ["sid1", "sid2"]
    plan = plan_artwork(ids, _source({"sid1": _png((200, 200, 30))}))
    _build_stick(root, ids, plan)

    assert plan.track_artwork_ids["sid2"] == 0, "if a coverless track gets a nonzero artwork id then broken"
    assert len(_artwork_files(root)) == 2, "if a coverless track gets a file then broken"
    bare = read_usb_export(root)["tracks"][1]
    assert bare["artwork_present"] is False and bare["artwork_path"] is None, (
        "if read_usb_export claims artwork for a coverless track then broken"
    )
    available = sl.open_stick_library(UUID, _Scan(root)).artwork_available
    assert 2 not in available, "if the stick browser does not show the placeholder for a coverless track then broken"


def test_artwork_files_are_in_the_hash_list_and_readback_verifies_them(tmp_path: Path) -> None:
    root = tmp_path / "STICK"
    plan = plan_artwork(["sid1"], _source({"sid1": _png((90, 90, 90))}))
    _build_stick(root, ["sid1"], plan)

    hashes = plan.file_hashes()
    expected = {
        "PIONEER/Artwork/00001/a1.jpg",
        "PIONEER/Artwork/00001/a1_m.jpg",
    }
    assert set(hashes) == expected, "if an artwork file is missing from the hash list then broken"
    for rel, digest in hashes.items():
        assert hashlib.sha256((root / rel).read_bytes()).hexdigest() == digest
    verify_artwork(plan, root)

    medium = root / "PIONEER/Artwork/00001/a1_m.jpg"
    data = bytearray(medium.read_bytes())
    data[-3] ^= 0xFF
    medium.write_bytes(bytes(data))
    with pytest.raises(ArtworkExportError) as mismatch:
        verify_artwork(plan, root)
    assert mismatch.value.code == "readback_hash_mismatch", "if read-back passes an altered file then broken"

    (root / "PIONEER/Artwork/00001/a1.jpg").unlink()
    with pytest.raises(ArtworkExportError) as missing:
        verify_artwork(plan, root)
    assert missing.value.code == "readback_missing", "if read-back passes a missing file then broken"


def test_undecodable_cover_fails_fast() -> None:
    with pytest.raises(ArtworkExportError) as caught:
        plan_artwork(["sid1"], _source({"sid1": b"not an image"}))
    assert caught.value.code == "artwork_undecodable", "if an undecodable cover is silently dropped then broken"


def test_write_refuses_an_existing_file(tmp_path: Path) -> None:
    plan = plan_artwork(["sid1"], _source({"sid1": _png((1, 2, 3))}))
    write_artwork(plan, tmp_path)
    with pytest.raises(ArtworkExportError) as caught:
        write_artwork(plan, tmp_path)
    assert caught.value.code == "artwork_target_exists", "if write_artwork overwrites a stick file then broken"


def test_bucket_folder_numbering() -> None:
    assert artwork_pdb_path(1) == "/PIONEER/Artwork/00001/a1.jpg"
    assert artwork_pdb_path(1000) == "/PIONEER/Artwork/00001/a1000.jpg"
    assert artwork_pdb_path(1001) == "/PIONEER/Artwork/00002/a1001.jpg", (
        "if the bucket folder numbering drifts then broken"
    )
    with pytest.raises(ValueError):
        artwork_pdb_path(0)


def test_medium_path_matches_stick_library_rule() -> None:
    assert medium_relative_path("/PIONEER/Artwork/00001/a7.jpg") == "PIONEER/Artwork/00001/a7_m.jpg", (
        "if the medium path disagrees with stick_library's _m rule then broken"
    )


# ----- agent parity (CLI) ----------------------------------------------------


def _cover_file(tmp_path: Path) -> Path:
    path = tmp_path / "cover.png"
    path.write_bytes(_png((120, 40, 160)))
    return path


def test_cli_artwork_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "STICK"
    root.mkdir()
    cover = _cover_file(tmp_path)
    code = main(["artwork", "--target", str(root), "--track", f"sid1={cover}", "--track", "sid2", "--no-library"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert len(out["rows"]) == 1 and out["track_artwork_ids"] == {"sid1": 1, "sid2": 0}
    assert len(out["file_hashes"]) == 2, "if the dry run omits the artwork hash list then broken"
    assert not any(root.iterdir()), "if the artwork dry run writes to the stick then broken"


def test_cli_artwork_apply_writes_and_verifies(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "STICK"
    root.mkdir()
    cover = _cover_file(tmp_path)
    code = main(["artwork", "--target", str(root), "--track", f"sid1={cover}", "--no-library", "--apply"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["verified"] is True, "if artwork --apply does not verify then broken"
    assert out["pdb_table"] == {"1": "/PIONEER/Artwork/00001/a1.jpg"}
    for rel, digest in out["files"].items():
        assert hashlib.sha256((root / rel).read_bytes()).hexdigest() == digest


def test_cli_artwork_refusal_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    code = main(["artwork", "--target", str(tmp_path), "--track", f"sid1={bad}", "--no-library"])
    assert code == 1
    assert json.loads(capsys.readouterr().err)["error"] == "artwork_undecodable"


def test_cli_artwork_bad_track_spec_exits_2(tmp_path: Path) -> None:
    assert main(["artwork", "--target", str(tmp_path), "--track", "sid1=", "--no-library"]) == 2, (
        "if a malformed --track is accepted then broken"
    )
