"""Windows-portability tests for :mod:`apps.webui.server.rb_vendor`.

Simulates win32 the same way ``tests/shared/test_platform_paths.py`` does --
monkeypatching :mod:`apps.shared.platform_paths`' own ``IS_DARWIN`` /
``IS_WINDOWS`` / ``SHARE_ROOT`` / ``load_path_map`` globals. ``rb_vendor``
imported the exact same function objects from that module, and a function's
``__globals__`` is the module's own (shared) dict, so monkeypatching an
attribute on ``apps.shared.platform_paths`` is visible to ``rb_vendor`` calls
without needing to reload or re-import anything -- except the one test that
exercises a full reimport under a simulated ``sys.platform`` to prove the
module has no import-time Mac-only path construction left in it.

This module must NOT use the ``requires_darwin`` / ``requires_audio_stack``
custom markers (W4 registers those in pyproject.toml; using them here before
registration would fail ``--strict-markers``) -- plain
``pytest.mark.skipif`` / ``pytest.importorskip`` only where a real-platform
check is actually needed (none of these tests need one).

Regression one-liners:
  - if the rekordbox adapter still carries its own hardcoded SHARE_ROOT then
    broken
  - if reimporting the adapter under a simulated win32 sys.platform touches a
    literal ~/Library/Pioneer path then broken
  - if MDT_DATA_DIR doesn't rederive DATA_DIR/MASTER_PLAIN_DB/
    ANLZ_CACHE_DIR/VOCAL_CACHE_DIR/STATE_DB then broken
  - if audio_file() fabricates a Path for an unmapped foreign-absolute
    FolderPath instead of raising a named-reason 404 then broken
  - if audio_file() doesn't resolve a /PIONEER/... path under the platform
    SHARE_ROOT then broken
  - if audio_file() doesn't resolve a path-map-covered FolderPath to its
    mapped local path then broken
  - if resolve_share_path (the legacy back-compat alias) raises instead of
    returning a bare Path for an unmapped path then broken
  - if a /PIONEER audio, artwork, or ANLZ path follows a symlink outside the
    platform SHARE_ROOT then broken
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from apps.adapters.rekordbox import config as rb_config
from apps.adapters.rekordbox import paths as rb_paths
from apps.shared import platform_paths as pp
from apps.webui import crate_sync
from apps.webui.server import rb_vendor


def _content(
    folder_path: str | None = None,
    *,
    image_path: str | None = None,
    analysis_data_path: str | None = None,
) -> rb_vendor.RbContent:
    return rb_vendor.RbContent(
        stable_id="sid-1",
        vendor_id="vid-1",
        folder_path=folder_path,
        image_path=image_path,
        analysis_data_path=analysis_data_path,
        length_s=None,
        comment=None,
        genre=None,
    )


def _foreign_prefix() -> str:
    """A path prefix that is FOREIGN on THIS interpreter's real platform, so
    the path-map / unmapped tests exercise the real branch without needing
    to simulate a different OS.

    Branches on IS_WINDOWS, matching ``pp._is_foreign_absolute``: everything
    non-win32 (macOS AND Linux) treats a drive-letter/UNC path as foreign, and
    only win32 treats a POSIX-rooted path as foreign. Branching on IS_DARWIN
    instead read "not macOS" as "Windows", so on the Linux CI runner this handed
    back ``/Users/dev/Music`` -- an ordinary native POSIX path there, not a
    foreign one -- and both callers silently exercised the local-file-missing
    branch instead of the unmapped branch they assert on.
    """
    prefix = "/Users/dev/Music" if pp.IS_WINDOWS else "D:/music-library"
    # Fail loudly rather than let a future platform silently re-run these tests
    # against the wrong branch, which is precisely how the IS_DARWIN bug hid.
    assert pp._is_foreign_absolute(f"{prefix}/x.mp3"), (
        f"{prefix!r} is not foreign on {pp.PLATFORM}; the unmapped/path-map "
        "tests would exercise the wrong branch"
    )
    return prefix


# ----- no hardcoded SHARE_ROOT left in this module ---------------------------

def test_module_has_no_hardcoded_share_root() -> None:
    """The old ``rb_vendor.SHARE_ROOT = Path.home()/Library/Pioneer/...``
    constant is gone; every resolution routes through the shared platform
    resolver instead."""
    assert not hasattr(rb_vendor, "SHARE_ROOT")
    assert not hasattr(rb_config, "SHARE_ROOT")
    assert not hasattr(rb_paths, "SHARE_ROOT")


def test_rb_vendor_reimports_cleanly_on_simulated_win32() -> None:
    """Full reimport under a simulated win32 ``sys.platform``: proves nothing
    at module scope constructs a literal ~/Library/Pioneer path -- the
    module delegates every OS-branching decision to platform_paths, which
    picks a %APPDATA%-rooted share on win32.

    Deliberately does NOT use ``monkeypatch`` for ``sys.platform``/``APPDATA``:
    monkeypatch only undoes those AFTER this function returns, which would
    leave the corrective ``finally``-block reload running while win32 is
    still simulated -- permanently freezing ``pp``/``rb_vendor`` in win32
    state for every test that runs afterward in this session. Restoring the
    real values FIRST, then reloading, is what actually restores reality.
    """
    real_platform = sys.platform
    real_appdata = os.environ.get("APPDATA")
    os.environ["APPDATA"] = r"C:\Users\dj\AppData\Roaming"
    sys.platform = "win32"
    try:
        reloaded_pp = importlib.reload(pp)
        reloaded_config = importlib.reload(rb_config)
        importlib.reload(rb_paths)
        reloaded_rb = importlib.reload(rb_vendor)
        assert reloaded_pp.IS_WINDOWS is True
        assert "Library" not in str(reloaded_pp.SHARE_ROOT)
        assert not hasattr(reloaded_rb, "SHARE_ROOT")
        assert not hasattr(reloaded_config, "SHARE_ROOT")
        assert "Library" not in str(reloaded_config.ANLZ_CACHE_DIR)
        mapped = reloaded_rb.resolve_library_path("/PIONEER/USB/track.mp3")
        assert mapped.resolved is not None
        assert mapped.mapped is True
        assert "Library" not in str(mapped.resolved)
        assert str(mapped.resolved).startswith(r"C:\Users\dj\AppData\Roaming")
    finally:
        sys.platform = real_platform
        if real_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = real_appdata
        importlib.reload(pp)
        importlib.reload(rb_config)
        importlib.reload(rb_paths)
        importlib.reload(rb_vendor)
        # crate_sync is reloaded for a different reason from the three above:
        # it is not asserted on here, but it binds pp.STREAMING_PREFIXES at
        # import time. Reloading pp rebinds that name to a NEW tuple, so any
        # module not reloaded after pp keeps a reference to the old object.
        # The values stay equal, only the identity diverges, which is exactly
        # what tests/shared/test_streaming_path_predicates.py asserts on
        # ("no module keeps a private copy"). Restoring reality therefore
        # means reloading every module that binds a pp name, not only the ones
        # this test looks at. Keep this list in step with the identity
        # assertions in that file.
        importlib.reload(crate_sync)


# ----- MDT_DATA_DIR override --------------------------------------------------

def test_mdt_data_dir_unset_keeps_default_layout() -> None:
    from apps.shared.paths import DATA_DIR as default_data_dir
    from apps.shared.paths import STATE_DB as default_state_db

    assert os.environ.get("MDT_DATA_DIR") is None
    assert rb_config.DATA_DIR == default_data_dir
    assert rb_config.STATE_DB == default_state_db
    assert rb_config.MASTER_PLAIN_DB == default_data_dir / "master.plain.db"


def test_mdt_data_dir_override_rederives_all_constants(tmp_path: Path) -> None:
    original = os.environ.get("MDT_DATA_DIR")
    os.environ["MDT_DATA_DIR"] = str(tmp_path)
    try:
        reloaded = importlib.reload(rb_config)
        assert reloaded.DATA_DIR == tmp_path
        assert reloaded.MASTER_PLAIN_DB == tmp_path / "master.plain.db"
        assert reloaded.ANLZ_CACHE_DIR == tmp_path / "state" / "anlz-cache"
        assert reloaded.VOCAL_CACHE_DIR == tmp_path / "state" / "vocal-cache"
        assert reloaded.STATE_DB == tmp_path / "state" / "state.db"
    finally:
        if original is None:
            os.environ.pop("MDT_DATA_DIR", None)
        else:
            os.environ["MDT_DATA_DIR"] = original
        importlib.reload(rb_config)


# ----- audio_file() explicit states -------------------------------------------

def test_audio_file_raises_404_on_unmapped_foreign_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FolderPath foreign to this OS with no PathMap entry is the explicit
    'unmapped' F3 availability state -- never a fabricated Path."""
    monkeypatch.delenv("MDT_PATH_MAP", raising=False)
    monkeypatch.setattr(pp, "load_path_map", lambda: pp.PathMap(entries=()))
    foreign_path = f"{_foreign_prefix()}/track.mp3"

    with pytest.raises(HTTPException) as exc_info:
        rb_vendor.audio_file(_content(foreign_path))

    assert exc_info.value.status_code == 404
    detail = exc_info.value.detail
    assert detail["code"] == "AUDIO_FILE_MISSING"
    assert "unmapped" in detail["message"]
    assert foreign_path in detail["message"]


def test_audio_file_resolves_pioneer_path_under_platform_share_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_share_root = tmp_path / "share"
    audio = fake_share_root / "PIONEER" / "USB" / "track.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"fake wav bytes")
    monkeypatch.setattr(pp, "SHARE_ROOT", fake_share_root)

    path, media_type = rb_vendor.audio_file(_content("/PIONEER/USB/track.wav"))

    assert path == audio
    assert media_type == "audio/wav"


def test_audio_file_rejects_share_root_symlink_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_share_root = tmp_path / "share"
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"not a library asset")
    audio = fake_share_root / "PIONEER" / "USB" / "track.wav"
    audio.parent.mkdir(parents=True)
    try:
        audio.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")
    monkeypatch.setattr(pp, "SHARE_ROOT", fake_share_root)

    with pytest.raises(HTTPException) as exc_info:
        rb_vendor.audio_file(_content("/PIONEER/USB/track.wav"))

    assert exc_info.value.status_code == 404
    assert "unsafe:share-symlink" in exc_info.value.detail["message"]


def test_artwork_file_rejects_derived_share_root_symlink_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_share_root = tmp_path / "share"
    artwork = fake_share_root / "PIONEER" / "USB" / "artwork.jpg"
    artwork.parent.mkdir(parents=True)
    artwork.write_bytes(b"safe source image")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"not artwork")
    thumbnail = artwork.with_name("artwork_s.jpg")
    try:
        thumbnail.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")
    monkeypatch.setattr(pp, "SHARE_ROOT", fake_share_root)

    with pytest.raises(HTTPException) as exc_info:
        rb_vendor.artwork_file(
            _content(image_path="/PIONEER/USB/artwork.jpg"), "s"
        )

    assert exc_info.value.status_code == 404
    assert "unsafe:share-symlink" in exc_info.value.detail["message"]


def test_analysis_paths_reject_share_root_symlink_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_share_root = tmp_path / "share"
    outside = tmp_path / "outside.DAT"
    outside.write_bytes(b"not an ANLZ file")
    analysis = fake_share_root / "PIONEER" / "USB" / "ANLZ0000.DAT"
    analysis.parent.mkdir(parents=True)
    try:
        analysis.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")
    monkeypatch.setattr(pp, "SHARE_ROOT", fake_share_root)
    analysis_path = "/PIONEER/USB/ANLZ0000.DAT"

    with pytest.raises(HTTPException) as exc_info:
        rb_vendor.anlz_dir(_content(analysis_data_path=analysis_path))

    assert exc_info.value.status_code == 404
    assert "unsafe:share-symlink" in exc_info.value.detail["message"]
    assert rb_vendor.preview_strip(analysis_path) == (None, None)
    assert rb_vendor.bulk_file_exists([analysis_path]) == {analysis_path: False}


def _minimal_pwav_dat(entries: int = 1200) -> bytes:
    """A minimal, real, parseable PWAV .DAT payload (PMAI container, one
    PWAV section). Byte-exact to the on-disk format `_read_pwav_mono` reads
    (`apps/webui/server/rb_vendor_pkg/anlz.py`): `PMAI` + u32 first-section
    offset, then per-section `fourcc` + u32 head_len + u32 total_len, PWAV's
    own u32 entry count at +12, then `entries` payload bytes starting at
    `off + head_len`. `entries` is >= PREVIEW_COLUMNS (120) so the peak
    downsample in `preview_strip` has enough columns to reduce from.
    """
    import struct as _struct

    head_len = 16
    section = (
        b"PWAV"
        + _struct.pack(">II", head_len, head_len + entries)
        + _struct.pack(">I", entries)
        + bytes((i * 7) % 32 for i in range(entries))
    )
    return b"PMAI" + _struct.pack(">I", 8) + section


def test_preview_strip_falls_back_past_a_broken_sibling_not_the_whole_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pin 66b9602c5887: a missing preview, reported live, traced to a single
    fallback candidate's containment failure aborting the ENTIRE PWV6 -> PWV4
    -> PWAV chain instead of ruling out only that one candidate.

    [if] the .2EX (PWV6) sibling resolves to a share-root symlink escape, but
         the .DAT (PWAV) sibling is a real, valid, in-bounds file [then]
         preview_strip must still return the PWAV preview - the analysis was
         never actually missing, only the FIRST fallback candidate was
         ⛔️ (None, None): a decodable fallback source sitting right next to
         the broken one was thrown away with it.
    """
    fake_share_root = tmp_path / "share"
    anlz_dir = fake_share_root / "PIONEER" / "USBANLZ" / "P1" / "00000001"
    anlz_dir.mkdir(parents=True)
    dat = anlz_dir / "ANLZ0000.DAT"
    dat.write_bytes(_minimal_pwav_dat())

    outside = tmp_path / "outside.2EX"
    outside.write_bytes(b"not a real PWV6 file")
    twoex = anlz_dir / "ANLZ0000.2EX"
    try:
        twoex.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")
    # ANLZ0000.EXT (PWV4) is deliberately absent, exercising the pre-existing
    # "file doesn't exist: continue" branch too, so all three fallback states
    # (symlink-escape, missing file, real hit) are covered in one chain walk.

    monkeypatch.setattr(pp, "SHARE_ROOT", fake_share_root)
    analysis_path = "/PIONEER/USBANLZ/P1/00000001/ANLZ0000.DAT"

    preview_b64, preview_max = rb_vendor.preview_strip(analysis_path)

    assert preview_b64 is not None, (
        "the .2EX containment failure must rule out only .2EX, not the whole "
        "chain - a real .DAT sibling one suffix later must still be served"
    )
    assert preview_max is not None and preview_max > 0
    import base64 as _b64

    raw = _b64.b64decode(preview_b64)
    assert len(raw) == 120 * 3, "preview strip must be uint8[120][3] regardless of source"


def test_audio_file_resolves_via_path_map_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A foreign-absolute FolderPath covered by a PathMap entry rewrites to
    the mapped local path -- the explicit relocation state (reason
    'path-map'), never a guess."""
    local_root = tmp_path / "relocated-library"
    audio = local_root / "artist - track.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"fake mp3 bytes")

    prefix = _foreign_prefix()
    foreign_path = f"{prefix}/artist - track.mp3"
    path_map = pp.PathMap(entries=((prefix, str(local_root)),))
    monkeypatch.setattr(pp, "load_path_map", lambda: path_map)

    path, media_type = rb_vendor.audio_file(_content(foreign_path))

    assert path == audio
    assert media_type == "audio/mpeg"


# ----- resolve_share_path back-compat alias -----------------------------------

def test_resolve_share_path_alias_never_raises_on_unmapped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_share_root = tmp_path / "share"
    monkeypatch.setattr(pp, "SHARE_ROOT", fake_share_root)
    assert (
        rb_vendor.resolve_share_path("/PIONEER/USB/x.mp3")
        == fake_share_root / "PIONEER" / "USB" / "x.mp3"
    )

    monkeypatch.setattr(pp, "load_path_map", lambda: pp.PathMap(entries=()))
    foreign_path = f"{_foreign_prefix()}/x.mp3"
    result = rb_vendor.resolve_share_path(foreign_path)
    assert result == Path(foreign_path)
    assert not result.is_file()

pytestmark = pytest.mark.rb_parity
