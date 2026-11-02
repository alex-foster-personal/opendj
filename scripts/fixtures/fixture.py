"""perf-lab fixture bundles: carry a few fully analyzed tracks to another machine.

Profiling on an empty or unanalyzed library skips the code that matters (beat
grids, phrases, key segments, waveform bands, cues drive most per-frame work),
so a cloud or test machine needs a handful of tracks exactly as a real library
holds them. This script moves them as one zip, stdlib only, any OS.

export (on the machine with the real library):
  python scripts/fixtures/fixture.py export --data-dir <repo>/data \
      --pick 6 --out ~/Desktop/odj-fixture.zip          # or --ids sid1,sid2
  Picks tracks whose audio exists and whose rekordbox ANLZ files (AnalysisDataPath
  in master.plain.db) are on disk, preferring 3-8 minute files, stopping at
  --max-audio-mb. Export maps those paths with the app's own resolver, so it
  runs from a checkout of this repo.

import (on the profiling machine):
  python scripts/fixtures/fixture.py import --zip odj-fixture.zip --data-dir /path/to/new/data
  Unpacks into a fresh data dir, then rewrites every stored path of the bundled
  tracks to the unpacked audio. The ANLZ files land in <data>/pioneer-share at
  their share-relative AnalysisDataPath with their original mtimes, so the
  backend serves them in remote library mode:
  MDT_LIBRARY_MODE=remote MDT_CRATE_ROOT=<data> python -m apps.engine_core serve --data-dir <data>
  Remote mode plays a Mac /Users/... path only through the path map; pack.py
  ensure maps <data> onto itself in <data>/path-map.json once the import is done.

The bundle holds: a consistent copy of state.db (VACUUM INTO) and of
master.plain.db when present, the bundled tracks' audio, every file under
state/ whose path names a bundled stable_id (anlz-cache, waveform caches,
stems with --with-stems), and manifest.json mapping each stable_id to its
original path. The state.db copy is the whole library's metadata (rows for
tracks without audio simply show as missing): treat the zip as private.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import posixpath
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

MANIFEST_VERSION = 1



def _ro_uri(db: Path) -> str:
    """Read-only SQLite URI; as_uri() percent-encodes ? and # so they stay in the path."""
    return Path(db).resolve().as_uri() + "?mode=ro"

def _vacuum_copy(src: Path, dst: Path) -> None:
    con = sqlite3.connect(_ro_uri(src), uri=True)
    try:
        con.execute("VACUUM INTO ?", (str(dst),))
    finally:
        con.close()


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _track_paths(data: Path, state_db: Path) -> dict[str, tuple[Path, str]]:
    """Each live track's audio as the app itself resolves it on this machine: (file to read, stored path).

    Delegates to the production resolver, so a tombstoned location, another
    machine's location or a deleted track is never packed where the app would not
    play it. The file to read is the mapped path (path-map.json, share root) that
    deck load opens; the stored path is what the databases hold, which import
    rewrites. The path map is the selected library's own, as production loads it.
    Only tracks whose audio is on disk are returned.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from apps.shared.platform_paths import load_path_map
    from apps.shared.state.locations import local_audio_path_raw, mapped_audio_path_if_materialised

    pm = load_path_map(data)

    con = sqlite3.connect(_ro_uri(state_db), uri=True)
    try:
        sids = [r[0] for r in con.execute("SELECT stable_id FROM tracks WHERE deleted_at IS NULL")]
        paths = {}
        for sid in sids:
            raw, _source = local_audio_path_raw(con, sid, path_map=pm)
            mapped = mapped_audio_path_if_materialised(raw, path_map=pm) if raw is not None else None
            if raw is not None and mapped is not None:
                paths[sid] = (mapped, raw)
        return paths
    finally:
        con.close()


def _duration_s(state_db: Path, sid: str) -> float | None:
    con = sqlite3.connect(_ro_uri(state_db), uri=True)
    try:
        cols = _columns(con, "tracks")
        for c in ("duration_s", "duration_ms", "duration"):
            if c in cols:
                v = con.execute(f"SELECT {c} FROM tracks WHERE stable_id=?", (sid,)).fetchone()
                if v and v[0]:
                    return float(v[0]) / (1000.0 if c == "duration_ms" else 1.0)
        return None
    finally:
        con.close()


def _state_files_for(state_dir: Path, sids: set[str], with_stems: bool) -> list[Path]:
    out: list[Path] = []
    # Linked roots are followed (state/stems on an external disk is a supported
    # layout, see apps/stems/bundle_lock.py), each real directory only once so a
    # link back up the tree cannot loop.
    seen: set[tuple[int, int]] = set()
    for root, dirs, files in os.walk(state_dir, followlinks=True):
        st = os.stat(root)
        if (st.st_dev, st.st_ino) in seen:
            dirs[:] = []
            continue
        seen.add((st.st_dev, st.st_ino))
        rel_root = Path(root).relative_to(state_dir)
        if not with_stems and rel_root.parts[:1] == ("stems",):
            dirs[:] = []
            continue
        hit_dir = any(s in str(rel_root) for s in sids)
        out.extend(Path(root) / f for f in files if hit_dir or any(s in f for s in sids))
    return out


SHARE_ARC = "pioneer-share"
LIBRARY_ROOTS = ("state", "audio", SHARE_ARC, "master.plain.db")  # what an import owns under --data-dir
REPLACED_PREFIX = ".fixture-import.replaced-"  # a library a --force import is replacing


def _anlz_sources(data: Path, state_db: Path, sids: list[str]) -> dict[str, list[tuple[Path, str]]]:
    """stable_id -> [(ANLZ file on disk, its zip name)] for tracks whose rekordbox analysis is present.

    The backend's GET /anlz reads these raw files through AnalysisDataPath (the
    anlz-cache JSON alone is not enough), so a pack carries them. Only the
    share-relative /PIONEER/ form is portable: remote library mode maps it under
    MDT_CRATE_ROOT/pioneer-share on the importing machine.
    """
    plain = data / "master.plain.db"
    if not plain.exists():
        return {}
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from apps.shared.platform_paths import load_path_map, resolve_asset_path  # the app's own mapping and containment

    pm = load_path_map(data)  # the selected library's map, as for its audio

    scon = sqlite3.connect(_ro_uri(state_db), uri=True)
    pcon = sqlite3.connect(_ro_uri(plain), uri=True)
    out: dict[str, list[tuple[Path, str]]] = {}
    try:
        for sid in sids:
            row = scon.execute(
                "SELECT vendor_id FROM track_vendor_ids WHERE stable_id=? AND vendor='rekordbox' AND deleted_at IS NULL", (sid,)
            ).fetchone()
            if not row:
                continue
            adp = pcon.execute(
                "SELECT AnalysisDataPath FROM djmdContent WHERE ID=? AND rb_local_deleted=0", (str(row[0]),)
            ).fetchone()
            if not adp or not adp[0] or not adp[0].startswith("/PIONEER/"):
                continue
            dat = resolve_asset_path(adp[0], path_map=pm).resolved
            if dat is None or not dat.is_file():
                continue
            rel_dir = Path(adp[0].lstrip("/")).parent.as_posix()
            out[sid] = [(f, f"{SHARE_ARC}/{rel_dir}/{f.name}") for f in sorted(dat.parent.glob("ANLZ*")) if f.is_file()]
    finally:
        scon.close()
        pcon.close()
    return out


def cmd_export(a: argparse.Namespace) -> int:
    data = Path(a.data_dir).expanduser().resolve()
    state = data / "state"
    state_db = state / "state.db"
    if not state_db.exists():
        print(f"[FAIL] no state.db at {state_db}", file=sys.stderr)
        return 2
    paths = _track_paths(data, state_db)
    if a.ids:
        chosen = list(dict.fromkeys(s for s in a.ids.split(",") if s))  # a repeat would bundle a member twice
        if not chosen:
            print(f"[FAIL] --ids {a.ids!r} names no track", file=sys.stderr)
            return 2
        missing = [s for s in chosen if s not in paths]
        if missing:
            print(f"[FAIL] no playable audio for {missing}", file=sys.stderr)
            return 2
        # A pack promises full analysis, so a hand-picked track needs its ANLZ too.
        anlz = _anlz_sources(data, state_db, chosen)
        bare = [s for s in chosen if s not in anlz]
        if bare and not a.allow_unanalyzed:
            print(f"[FAIL] no rekordbox ANLZ files on disk for {bare}; analyze them first or pass --allow-unanalyzed", file=sys.stderr)
            return 2
    else:
        playable = list(paths)
        anlz = _anlz_sources(data, state_db, playable)
        cands = []
        for sid in playable:
            p = paths[sid][0]
            if sid not in anlz:
                continue
            dur = _duration_s(state_db, sid) or 0
            score = 0 if 180 <= dur <= 480 else 1
            cands.append((score, p.stat().st_size, sid))
        cands.sort()
        chosen, total = [], 0
        for _, size, sid in cands:
            if len(chosen) >= a.pick:
                break
            if total + size > a.max_audio_mb * 1048576:
                continue
            chosen.append(sid)
            total += size
        if not chosen:
            print("[FAIL] no track has both audio and rekordbox ANLZ files on disk; pass --ids", file=sys.stderr)
            return 2
    out = Path(a.out).expanduser()
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        _vacuum_copy(state_db, t / "state.db")
        plain = data / "master.plain.db"
        if plain.exists():
            _vacuum_copy(plain, t / "master.plain.db")
        manifest: dict[str, Any] = {
            "version": MANIFEST_VERSION,
            "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "tracks": {},
            "state_files": [],
            "has_master_plain_db": plain.exists(),
            # zip name -> mtime: the anlz-cache entry is keyed on the ANLZ files' mtime
            "anlz_files": {},
        }
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
            z.write(t / "state.db", "state/state.db")
            if plain.exists():
                z.write(t / "master.plain.db", "master.plain.db")
            for sid in chosen:
                src, stored = paths[sid]
                arc = f"audio/{sid}{src.suffix.lower()}"
                z.write(src, arc)
                manifest["tracks"][sid] = {"original_path": stored, "bundle_path": arc, "bytes": src.stat().st_size,
                                           "anlz": [n for _, n in anlz.get(sid, [])]}
            for sid in chosen:
                for src, arc in anlz.get(sid, []):
                    z.write(src, arc)
                    manifest["anlz_files"][arc] = src.stat().st_mtime
            for f in _state_files_for(state, set(chosen), a.with_stems):
                arc = "state/" + f.relative_to(state).as_posix()
                z.write(f, arc)
                manifest["state_files"].append(arc)
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
    mb = out.stat().st_size / 1048576
    print(f"[OK] {len(chosen)} tracks, {len(manifest['state_files'])} state files, {mb:.0f} MB -> {out}")
    for sid, row in manifest["tracks"].items():
        print(f"     {sid}  {Path(row['original_path']).name}")
    return 0


def _rewrite_state_paths(con: sqlite3.Connection, manifest: dict[str, Any], data: Path) -> int:
    """Point state.db's audio paths at the unpacked files; returns the tracks rows rewritten.

    Import runs this on the real copy and check runs it on a scratch copy, so a
    schema the writes cannot apply to is refused before anything is removed.
    """
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rewritten = 0
    for sid, row in manifest["tracks"].items():
        new = str((data / row["bundle_path"]).resolve())
        rewritten += con.execute("UPDATE tracks SET file_path=? WHERE stable_id=?", (new, sid)).rowcount
        if "track_locations" in tables:
            con.execute(
                "UPDATE track_locations SET file_path=?, available=1 WHERE stable_id=? AND kind='local' AND file_path=?",
                (new, sid, row["original_path"]),
            )
            # The bundle carries this one file: any other location row (another
            # copy on the publisher's machine, a remote URL) is audio the app's
            # picker could choose instead, so the track keeps only its own.
            con.execute("DELETE FROM track_locations WHERE stable_id=? AND (file_path IS NULL OR file_path != ?)",
                        (sid, new))
    return rewritten


def _rekordbox_ids(con: sqlite3.Connection, sids: list[str]) -> dict[str, str]:
    """stable_id -> its live rekordbox vendor id, from state.db (empty when the table is absent)."""
    if "track_vendor_ids" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        return {}
    marks = ",".join("?" * len(sids))
    return {sid: str(vid) for sid, vid in con.execute(
        f"SELECT stable_id, vendor_id FROM track_vendor_ids WHERE vendor='rekordbox' AND deleted_at IS NULL"
        f" AND stable_id IN ({marks})", sids)}


def _rewrite_master_paths(pcon: sqlite3.Connection, manifest: dict[str, Any], data: Path, rb_ids: dict[str, str]) -> None:
    """Point each bundled track's own djmdContent row at its unpacked file.

    The row is the one state.db names by rekordbox id, whatever FolderPath it
    holds: export may have found the audio through a track_locations fallback
    while FolderPath was stale, and matching on a path would either miss that
    row or redirect every row sharing the path (an empty one, say).
    """
    if "FolderPath" not in _columns(pcon, "djmdContent"):
        return
    for sid, row in manifest["tracks"].items():
        if sid in rb_ids:
            pcon.execute("UPDATE djmdContent SET FolderPath=? WHERE CAST(ID AS TEXT)=?",
                         (str((data / row["bundle_path"]).resolve()), rb_ids[sid]))


# 2100-01-01T00:00:00Z. Every filesystem a pack lands on must store this exactly:
# ext4 silently clamps past 2446, so a larger bound would pass check and then lose
# the timestamp the ANLZ cache key depends on.
_MAX_MTIME = 4102444800
# macOS refuses any path of 1024 bytes or more (PATH_MAX counts the NUL), Linux
# 4096. Export writes member names of about a hundred bytes, and SQLite opens no
# database under a data dir much past 500 bytes (see _data_dir_problem), so a
# member cap of 512 keeps check predictive for any data dir import can use;
# import also measures each full destination against the macOS limit itself.
_MAX_MEMBER_BYTES = 512
_MAX_PATH_BYTES = 1024
# Everything export writes, and so everything the no---force guard and --force
# cleanup cover: any other member would land on an unguarded path.
_ROOT_DIRS = ("state/", "audio/", SHARE_ARC + "/")
_ROOT_FILES = ("manifest.json", "master.plain.db")


def _untraced_anlz(pcon: sqlite3.Connection, manifest: dict[str, Any], rb_ids: dict[str, str]) -> str | None:
    """Why a track's ANLZ claim is not where its rekordbox row sends the app, or None.

    The backend reaches a track's analysis through its rekordbox vendor id and that
    ID's live djmdContent.AnalysisDataPath under pioneer-share, so a live row
    pointing anywhere but the claimed ANLZ directory hides the bundled files. A
    track with no rekordbox link or no live row has no analysis the app reads
    (adopt --allow-unanalyzed records it as such) and is left to pack.py.
    """
    for sid, vid in rb_ids.items():
        claims = manifest["tracks"][sid].get("anlz") or []
        if not claims:
            continue
        row = pcon.execute("SELECT AnalysisDataPath FROM djmdContent WHERE CAST(ID AS TEXT)=? AND rb_local_deleted=0",
                           (vid,)).fetchone()
        if row is None:
            continue
        adp = row[0]
        dat = f"{SHARE_ARC}/{adp.lstrip('/')}" if isinstance(adp, str) and adp.startswith("/PIONEER/") else None
        if dat is None or dat not in claims or any(c.rsplit("/", 1)[0] != dat.rsplit("/", 1)[0] for c in claims):
            return (f"track {sid}: master.plain.db AnalysisDataPath {adp!r} does not reach its bundled ANLZ "
                    f"{sorted(claims)[:3]}")
    return None


def _bundled_library_problem(z: zipfile.ZipFile, manifest: dict[str, Any]) -> str | None:
    """Why the bundle's databases cannot be imported, or None when they can."""
    members = set(z.namelist())
    if "state/state.db" not in members:
        return "bundle has no state/state.db"
    # Import rewrites master.plain.db only when the manifest says it is there, so a
    # bundled one the flag denies would keep the publisher's FolderPaths.
    if bool(manifest.get("has_master_plain_db")) != ("master.plain.db" in members):
        return "manifest has_master_plain_db does not match whether the bundle holds master.plain.db"
    sids = list(manifest["tracks"])
    rb_ids: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("state/state.db", "master.plain.db"):  # state first: it names the master rows
            if name not in members:
                continue
            db = Path(tmp) / Path(name).name
            with z.open(name) as src, open(db, "wb") as dst:  # chunked: a real state.db is gigabytes
                shutil.copyfileobj(src, dst, 1 << 20)
            con = sqlite3.connect(db)
            try:
                # Apply the import's own writes to this scratch copy: a schema they
                # cannot run against fails here, before --force removes anything.
                if name == "master.plain.db":
                    con.execute("SELECT ID, FolderPath FROM djmdContent LIMIT 1").fetchall()
                    _rewrite_master_paths(con, manifest, Path(tmp), rb_ids)
                    if stray := _untraced_anlz(con, manifest, rb_ids):
                        return stray
                    continue
                rb_ids = _rekordbox_ids(con, sids)
                _rewrite_state_paths(con, manifest, Path(tmp))
                marks = ",".join("?" * len(sids))
                # A tombstoned row is not a track the library lists.
                listed = {r[0] for r in con.execute(
                    f"SELECT stable_id FROM tracks WHERE deleted_at IS NULL AND stable_id IN ({marks})", sids)}
            except sqlite3.DatabaseError as e:
                return f"bundled {name} is not readable ({e})"
            finally:
                con.close()
            missing = sorted(set(sids) - listed)
            if missing:
                return f"state.db has no live tracks row for {missing}"
    return None


_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"{p}{i}" for p in ("COM", "LPT") for i in [*"123456789", "\u00b9", "\u00b2", "\u00b3"])}


def _windows_component_problem(component: str) -> bool:
    """Whether Windows or ext4 refuses this path component: reserved characters or device names, a trailing dot or space, or an overlong name."""
    return (any(ch in '<>:"|?*' or ord(ch) < 32 for ch in component)
            # ext4 caps a name at 255 bytes and NTFS at 255 UTF-16 units; the larger count binds.
            or max(len(component.encode("utf-8")), len(component.encode("utf-16-le")) // 2) > 255
            or component.endswith((".", " "))
            or component.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_RESERVED)


def _fold(name: str) -> str:
    """The name as a case- and normalization-insensitive filesystem compares it.

    casefold alone leaves NFC and NFD spellings of one name distinct, though
    macOS treats them as the same file.
    """
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


def _member_name_problem(names: list[str]) -> str | None:
    """Why the zip's member names cannot all be extracted to distinct paths, or None."""
    # Export writes POSIX names; a backslash is a separator on Windows, so it is refused.
    if backslashed := sorted(n for n in names if "\\" in n):
        return f"bundle member names contain a backslash: {backslashed[:5]}"
    # Only canonical names are compared below: `a/./b`, `a/x/../b` and `a//b` all
    # extract onto `a/b`, so names holding such segments are refused outright.
    if odd := sorted(n for n in names if any(seg in ("", ".", "..") for seg in n.rstrip("/").split("/"))):
        return f"bundle member names are not canonical: {odd[:5]}"
    if bad_win := sorted(n for n in names if any(_windows_component_problem(c) for c in n.rstrip("/").split("/"))):
        return f"bundle member names cannot be created on every platform: {bad_win[:5]}"
    # Every component can be valid while the whole name is too long to create.
    if long_names := sorted(n for n in names if len(n.encode()) > _MAX_MEMBER_BYTES):
        return f"bundle member names are longer than {_MAX_MEMBER_BYTES} bytes: {[n[:60] + '...' for n in long_names[:3]]}"
    # Two file members that land on one path, or a file member that is also another
    # member's parent directory, cannot both be extracted, and extraction runs after
    # --force has cleared the target. Names are compared case-folded and Unicode
    # normalized, as on the default macOS and Windows filesystems.
    file_names = [_fold(n.rstrip("/")) for n in names if not n.endswith("/")]
    if dupes := sorted({n for n in file_names if file_names.count(n) > 1}):
        return f"bundle members collide on a case-insensitive filesystem: {dupes[:5]}"
    files = set(file_names)
    # A directory is either an explicit entry (`extra/`) or implied by a member below it.
    dirs = {_fold(n.rstrip("/")) for n in names if n.endswith("/")}
    dirs |= {p.as_posix() for n in names for p in PurePosixPath(_fold(n.rstrip("/"))).parents if p.as_posix() != "."}
    if clash := sorted(files & dirs):
        return f"bundle member is both a file and a directory: {clash[:5]}"
    return None


def _member_path_problem(members: set[str], data: Path) -> str | None:
    """Why some member would land where import cannot guard or create it, or None."""
    if stray := sorted(n for n in members if n not in _ROOT_FILES and not n.startswith(_ROOT_DIRS)):
        return f"bundle members outside {[*_ROOT_FILES, *_ROOT_DIRS]}: {stray[:5]}"
    if too_long := sorted(n for n in members if len(os.fsencode(data / n)) >= _MAX_PATH_BYTES):
        return f"bundle members would reach the {_MAX_PATH_BYTES}-byte path limit under {data}: {[n[:60] for n in too_long[:3]]}"
    return None


def _bundle_problem(z: zipfile.ZipFile, manifest: dict[str, Any], data: Path) -> str | None:
    """Why this bundle cannot be imported into data, or None when it can.

    Everything the import later relies on is checked here, before --force clears
    anything, and `check` runs the same function so a pack is never registered
    with a bundle its importer would refuse.
    """
    if manifest.get("version") != MANIFEST_VERSION:
        return f"manifest version {manifest.get('version')} != {MANIFEST_VERSION}"
    if not isinstance(manifest.get("tracks"), dict) or not manifest["tracks"]:
        return "manifest lists no tracks"
    # Import rewrites each row's paths using both fields, so both must be strings.
    malformed = [sid for sid, row in manifest["tracks"].items()
                 if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ("bundle_path", "original_path"))]
    if malformed:
        return f"manifest track rows lack bundle_path/original_path strings: {malformed[:5]}"
    # Every path the manifest names is used after extraction (paths are rewritten
    # to bundle_path, mtimes set on anlz_files, state files expected on disk), so
    # each must be one of the bundled members; members are contained below.
    members = set(z.namelist())
    file_members = {n for n in members if not n.endswith("/")}
    anlz = manifest.get("anlz_files", {})
    named = ([row.get("bundle_path") for row in manifest["tracks"].values()] + list(anlz)
             + list(manifest.get("state_files", [])))
    # A directory entry is a member too, but the manifest's paths must be files.
    stray = [n for n in named if not isinstance(n, str) or n not in file_members or n == "manifest.json"]
    if stray:
        return f"manifest names paths that are not bundled members: {stray[:5]}"
    # A track's anlz list is what marks it analyzed (ensure then wants its rekordbox
    # row), so each entry must be an ANLZ file the bundle carries, mtime and all.
    claims = {sid: row.get("anlz", []) for sid, row in manifest["tracks"].items()}
    unbundled = sorted(sid for sid, names in claims.items()
                       if not isinstance(names, list) or any(not isinstance(n, str) or n not in anlz for n in names))
    if unbundled:
        return f"track anlz entries are not bundled anlz_files for {unbundled[:5]}"
    # Import points tracks.file_path at bundle_path, so it must be the audio export
    # wrote under audio/, never a database or state file that also happens to be a member.
    if not_audio := sorted(sid for sid, row in manifest["tracks"].items() if not row["bundle_path"].startswith("audio/")):
        return f"bundle_path is not under audio/ for {not_audio[:5]}"
    # os.utime applies these after extraction, so each must be a time every platform
    # stores exactly: Windows refuses negative timestamps, and ext4 clamps huge ones.
    bad_mtime = [n for n, t in anlz.items()
                 if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t)
                 or not 0 <= t <= _MAX_MTIME]
    if bad_mtime:
        return f"anlz_files has mtimes that are not applicable timestamps for {bad_mtime[:5]}"
    # Lexical, not resolved: every member lies under a library root that import
    # requires absent or --force removes first (a link included, never followed),
    # so resolving here would follow a link that is about to go. A name that ".."
    # keeps inside data is refused below with the other names that are not canonical.
    unsafe = [n for n in members if n != "manifest.json"
              and (PurePosixPath(n).is_absolute() or bool(PureWindowsPath(n).drive)
                   or posixpath.normpath(n).split("/")[0] == "..")]
    if unsafe:
        return f"unsafe path in bundle: {unsafe[0]}"
    if problem := _member_name_problem(z.namelist()) or _member_path_problem(members, data):
        return problem
    # Names alone prove nothing about the bytes: read every member against its CRC,
    # so a damaged archive fails here rather than midway through extraction.
    try:
        bad = z.testzip()
    except (zipfile.BadZipFile, OSError, EOFError) as e:
        return f"bundle is damaged ({e})"
    if bad is not None:
        return f"bundle member {bad} is damaged (CRC mismatch)"
    return _bundled_library_problem(z, manifest)


def cmd_check(a: argparse.Namespace) -> int:
    """Validate a bundle exactly as import would, writing nothing."""
    with zipfile.ZipFile(a.zip) as z, tempfile.TemporaryDirectory() as tmp:
        problem = _bundle_problem(z, json.loads(z.read("manifest.json")), Path(tmp).resolve())
    if problem:
        print(f"[FAIL] {problem}", file=sys.stderr)
        return 2
    print("[OK] bundle imports")
    return 0


def _remove(path: Path) -> None:
    """Remove path whatever it is: a directory tree, a file, or a link (never followed)."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _data_dir_problem(data: Path) -> str | None:
    """Why the libraries cannot be opened under data, or None when they can.

    SQLite refuses database paths past its own limit (about 500 bytes on Linux),
    so a long enough data dir would extract every file and then fail on state.db.
    A scratch database at least as long a path as either library's is opened here,
    before --force removes anything.
    """
    # Named for this run alone, so a file of the same name already there is never
    # touched; the name is longer than state/state.db and master.plain.db.
    fd, name = tempfile.mkstemp(prefix=".fixture-probe-", suffix=".db", dir=data)
    os.close(fd)
    probe = Path(name)
    try:
        con = sqlite3.connect(probe)
        try:
            con.execute("PRAGMA user_version = 1")  # writes the header, as creating a library would
        finally:
            con.close()
    except sqlite3.Error as e:
        return f"SQLite cannot open a database under {data} ({e}); use a shorter --data-dir"
    finally:
        probe.unlink(missing_ok=True)
    return None


def _put_back_replaced(aside: Path, data: Path, moved: list[str], installing: bool) -> None:
    """Move the roots a failed forced import set aside back, clearing what it wrote first.

    Before installing began only the move itself can have failed: the roots not yet
    moved are still the earlier library's and stay where they are.
    """
    if installing:
        for root in LIBRARY_ROOTS:
            _remove(data / root)
    for root in moved:
        (aside / root).rename(data / root)
    if aside.exists():
        aside.rmdir()


def cmd_import(a: argparse.Namespace) -> int:
    data = Path(a.data_dir).expanduser().resolve()
    # A forced import killed midway leaves the library it replaced here; only a
    # person can tell whether to restore or drop it, so nothing runs over it.
    replaced = sorted(data.glob(f"{REPLACED_PREFIX}*")) if data.is_dir() else []
    if replaced:
        print(f"[FAIL] {replaced[0]} holds the library an interrupted --force import replaced; "
              f"move its contents back into {data} or delete it, then rerun", file=sys.stderr)
        return 2
    # Extraction overwrites, and --force replaces, these; without --force none may be there.
    # A link counts even when it dangles: extraction would write through it.
    held = [n for n in LIBRARY_ROOTS if os.path.lexists(data / n)]
    if held and not a.force:
        print(f"[FAIL] {data} already holds {held}; pass --force to overwrite", file=sys.stderr)
        return 2
    data.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(a.zip) as z:
        manifest = json.loads(z.read("manifest.json"))
        problem = _bundle_problem(z, manifest, data) or _data_dir_problem(data)
        if problem:
            print(f"[FAIL] {problem}", file=sys.stderr)
            return 2
        # A forced import replaces the library outright: anything the earlier one
        # left (stems, lyrics, ANLZ siblings) must not outlive it. It is renamed
        # aside, not deleted, until the new one is complete, so a failure anywhere
        # below (disk full, permissions, SQLite) puts it back.
        aside = data / f"{REPLACED_PREFIX}{os.getpid()}"
        moved: list[str] = []
        installing = False
        try:
            if held:
                aside.mkdir()
                for root in held:
                    (data / root).rename(aside / root)
                    moved.append(root)
            installing = True
            rewritten = _install(z, manifest, data)
        except BaseException:
            if held:
                try:
                    _put_back_replaced(aside, data, moved, installing)
                except OSError as e:
                    print(f"[FAIL] the import failed and putting the earlier library back failed too ({e}); "
                          f"it is in {aside}", file=sys.stderr)
                    raise
                print(f"[FAIL] the import failed; the earlier library was put back in {data}", file=sys.stderr)
            raise
    if held:
        _remove(aside)
    ok = [sid for sid, row in manifest["tracks"].items() if (data / row["bundle_path"]).exists()]
    print(f"[OK] {len(ok)} of {len(manifest['tracks'])} tracks unpacked, {rewritten} track paths rewritten -> {data}")
    print(f"     start: python -m apps.engine_core serve --data-dir {data}")
    print(f"     stable_ids: {','.join(ok)}")
    return 0 if len(ok) == len(manifest["tracks"]) else 1


def _install(z: zipfile.ZipFile, manifest: dict[str, Any], data: Path) -> int:
    """Extract a checked bundle into data and point its libraries there; returns the paths rewritten."""
    for name in z.namelist():
        if name == "manifest.json":
            continue
        if name.endswith("/"):  # a directory entry
            (data / name).mkdir(parents=True, exist_ok=True)
            continue
        dest = (data / name).resolve()  # contained: _bundle_problem checked every member
        dest.parent.mkdir(parents=True, exist_ok=True)
        with z.open(name) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst)
    for arc, mtime in manifest.get("anlz_files", {}).items():
        os.utime(data / arc, (mtime, mtime))  # keeps the bundled anlz-cache entries valid
    for stale in ("state.db-wal", "state.db-shm"):
        (data / "state" / stale).unlink(missing_ok=True)
    con = sqlite3.connect(data / "state" / "state.db")
    try:
        rb_ids = _rekordbox_ids(con, list(manifest["tracks"]))
        rewritten = _rewrite_state_paths(con, manifest, data)
        con.commit()
    finally:
        con.close()
    if manifest.get("has_master_plain_db"):
        pcon = sqlite3.connect(data / "master.plain.db")
        try:
            _rewrite_master_paths(pcon, manifest, data, rb_ids)
            pcon.commit()
        finally:
            pcon.close()
    return rewritten


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--data-dir", required=True)
    e.add_argument("--out", required=True)
    e.add_argument("--ids", default="")
    e.add_argument("--pick", type=int, default=6)
    e.add_argument("--max-audio-mb", type=float, default=400)
    e.add_argument("--with-stems", action="store_true")
    e.add_argument("--allow-unanalyzed", action="store_true",
                   help="let --ids name tracks with no anlz-cache entry (tests only; publish never passes it)")
    i = sub.add_parser("import")
    i.add_argument("--zip", required=True)
    i.add_argument("--data-dir", required=True)
    i.add_argument("--force", action="store_true")
    c = sub.add_parser("check", help="validate a bundle exactly as import would, writing nothing")
    c.add_argument("--zip", required=True)
    a = ap.parse_args()
    return {"export": cmd_export, "import": cmd_import, "check": cmd_check}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
