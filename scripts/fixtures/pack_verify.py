from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

try:
    from scripts.fixtures.pack_bundle import _library, _unreachable_analysis
    from scripts.fixtures.pack_cache import _read_meta, _whole_zip, _write_atomic
    from scripts.fixtures.pack_common import (
        DISCARD_PREFIX,
        IMPORTER_ASSET,
        MASTER_RECORD,
        META_ASSET,
        MOVED,
        NOT_EXTRACTED,
        OWNED,
        SHARE_PREFIX,
        SHARED,
        STARTING_PREFIX,
        ZIP_ASSET,
        ZIP_STAMP,
        LOCATION_RECORD,
        _sha256,
    )
except ImportError:
    from pack_bundle import _library, _unreachable_analysis
    from pack_cache import _read_meta, _whole_zip, _write_atomic
    from pack_common import (
        DISCARD_PREFIX,
        IMPORTER_ASSET,
        MASTER_RECORD,
        META_ASSET,
        MOVED,
        NOT_EXTRACTED,
        OWNED,
        SHARE_PREFIX,
        SHARED,
        STARTING_PREFIX,
        ZIP_ASSET,
        ZIP_STAMP,
        LOCATION_RECORD,
        _sha256,
    )


def _pack_id(meta: dict) -> tuple:
    """What an import is keyed on: the zip, its importer, and the track ids pack.json advertises.

    The tracks pack.json calls unanalyzed are part of it too: the warm path skips
    their rekordbox checks, so a list grown since the import must not be trusted.

    A new tag that ships the same zip with a corrected fixture.py must re-import.
    The ids are checked against the zip's own manifest before a marker records
    them, so a pack.json edited afterwards no longer matches and is re-checked.
    """
    return (
        meta["sha256"],
        meta["importer_sha256"],
        tuple(meta["stable_ids"]),
        tuple(sorted(meta.get("unanalyzed") or ())),
    )


def _done_id(done: Path) -> tuple | None:
    """_pack_id of what the completion marker says was imported, or None if absent or unreadable."""
    try:
        return _pack_id(json.loads(done.read_text(encoding="utf-8")))
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return None


def _stamp(p: Path) -> list[int]:
    """What changes whenever a file's bytes are replaced: the change time cannot be set back by hand."""
    st = p.stat()
    return [st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino]


def _stamp_proves_unchanged() -> bool:
    """Whether a matching _stamp shows the file's bytes were not rewritten.

    Platform seam: on Windows st_ctime_ns is the creation time, not a change
    time, so a same-length rewrite in place with its mtime set back leaves the
    whole stamp as it was. There the zip is hashed on every warm run instead.
    """
    return sys.platform != "win32"


def _zip_still_verified(zpath: Path, done: Path, meta: dict) -> bool:
    """Whether the cached zip is still the one pack.json's sha256 names.

    Hashing a real pack's zip takes seconds, so it is hashed only when its stamp
    differs from the one recorded when it was last hashed; a match is recorded.
    """
    try:
        recorded = json.loads(done.read_text(encoding="utf-8")).get(ZIP_STAMP)
        now = _stamp(zpath)
    except (OSError, ValueError, AttributeError):
        return False
    if recorded == now and _stamp_proves_unchanged():
        return True
    if _sha256(zpath) != meta["sha256"]:
        return False
    marker = json.loads(done.read_text(encoding="utf-8"))
    _write_atomic(done, json.dumps({**marker, ZIP_STAMP: now}, indent=1))
    return True


def _done_record(done: Path, key: str = MASTER_RECORD) -> list[list[str | None]] | None:
    """A record the completion marker holds (MASTER_RECORD or LOCATION_RECORD), or None."""
    try:
        got = json.loads(done.read_text(encoding="utf-8")).get(key)
    except (OSError, ValueError, AttributeError):
        return None
    return got if isinstance(got, list) else None


def _describes_zip(meta: dict, zpath: Path) -> bool:
    """Whether pack.json's track ids, manifest version and unanalyzed list are the zip's own.

    The unanalyzed list is recomputed from the bundled databases, as adopt computed
    it: the warm path skips those tracks' rekordbox checks, so a list edited since
    adopt must not be carried into the completion marker.
    """
    try:
        with zipfile.ZipFile(zpath) as z:
            manifest = json.loads(z.read("manifest.json"))
            if sorted(manifest["tracks"]) != list(meta["stable_ids"]) or manifest.get("version") != meta.get(
                "manifest_version"
            ):
                return False
            _listed, linked = _library(z, manifest["tracks"])
            return list(meta.get("unanalyzed") or []) == _unreachable_analysis(manifest, set(z.namelist()), linked)
    except (OSError, zipfile.BadZipFile, KeyError, ValueError, AttributeError, TypeError, sqlite3.DatabaseError):
        return False


def _drop_mismatched(tag: str, d: Path) -> None:
    """Clear a pack whose bytes disagree with its pack.json, without trusting one copy's metadata for another.

    The session copy goes whole (zip, importer and pack.json), since any of the
    three may be the damaged one. A shared asset goes only when it disagrees with
    the shared pack.json, so a damaged session cache never destroys the project
    copy, and a damaged project copy is not copied straight back on the rerun.
    """
    for n in (ZIP_ASSET, IMPORTER_ASSET, META_ASSET):
        (d / n).unlink(missing_ok=True)
    shared = SHARED / tag
    if shared == d:
        return
    smeta = _read_meta(shared)
    # Only a whole project copy can be judged: a short one may still be uploading
    # from another session, and deleting it there would cost every session a refetch.
    if smeta is None or not _whole_zip(shared, smeta):
        return
    # Byte hashes only prove the assets match pack.json; a pack.json whose ids or
    # version disagree with its own zip would be copied back unchanged on every rerun.
    if (shared / ZIP_ASSET).exists() and not _describes_zip(smeta, shared / ZIP_ASSET):
        for n in (ZIP_ASSET, IMPORTER_ASSET, META_ASSET):
            (shared / n).unlink(missing_ok=True)
        return
    for n, key in ((ZIP_ASSET, "sha256"), (IMPORTER_ASSET, "importer_sha256")):
        f = shared / n
        if f.exists() and _sha256(f) != smeta[key]:
            f.unlink()


def _hydration_gaps(
    zpath: Path,
    data: Path,
    recorded: list[list[str | None]] | None,
    unanalyzed: set[str],
    locations: list[list[str | None]] | None,
) -> list[str]:
    """Files the pack put in data that are now gone or no longer the bytes it unpacked.

    The done marker records what was unpacked, not what is still there, so a
    deleted state.db, audio file or ANLZ sibling would otherwise go unnoticed.
    """
    # The databases are rewritten on import and opened read-write by the engine, so
    # they are checked as whole databases rather than against the zip's bytes.
    # Everything else the import only extracts (audio, ANLZ, the state files such as
    # stems, caches and lyrics, and any other member) must still be the zip's bytes, size first (cheap),
    # then CRC-32 against the zip's own. A cache the app has since rewritten counts
    # as altered too, so the next ensure restores the pack's bytes.
    try:
        with zipfile.ZipFile(zpath) as z:
            sizes = {i.filename: (i.file_size, i.CRC) for i in z.infolist() if not i.is_dir()}
            manifest = json.loads(z.read("manifest.json"))
        present = ["state/state.db"]
        if manifest.get("has_master_plain_db"):
            present.append("master.plain.db")
        present = [str(n) for n in present]
        # Every member the import extracts, listed in the manifest or not: an extra
        # file under an allowed root lands in data just the same.
        want = [n for n in sizes if n not in (*NOT_EXTRACTED, *present)]
        bundled = {str(sid): str(r["bundle_path"]) for sid, r in manifest["tracks"].items()}
        # Export found these tracks' ANLZ through their rekordbox link, so the link must last.
        # adopt --allow-unanalyzed recorded the claims it could not trace (pack.json
        # "unanalyzed"); importing cannot create the link or row such a track lacks.
        analyzed = {str(sid) for sid, r in manifest["tracks"].items() if r.get("anlz")} - unanalyzed
        mtimes = {str(n): float(t) for n, t in manifest.get("anlz_files", {}).items()}
    except (OSError, zipfile.BadZipFile, KeyError, ValueError, AttributeError, TypeError):
        # A damaged or malformed cached zip is a gap too: the import path then
        # checks its sha256 against pack.json and drops it for a refetch.
        return [str(zpath)]
    gaps = [n for n in present if not (data / n).is_file()]
    gaps += _library_gaps(data, bundled, analyzed, [n for n in present if n not in gaps], recorded, locations)
    for name in want:
        f = data / name
        size, crc = sizes.get(name, (None, None))
        if not f.is_file() or f.stat().st_size != size or _crc32(f) != crc:
            gaps.append(name)
        elif name in mtimes and abs(f.stat().st_mtime - mtimes[name]) > 1e-3:
            gaps.append(name)  # the ANLZ cache entries the pack carries are keyed by these mtimes
    return gaps


def _library_gaps(
    data: Path,
    bundled: dict[str, str],
    analyzed: set[str],
    present: list[str],
    recorded: list[list[str | None]] | None,
    locations: list[list[str | None]] | None,
) -> list[str]:
    """The databases that no longer serve every pack track where it was unpacked.

    Every pack track must be a live row in state.db whose file_path is its file
    under data, as the import wrote it: a copied or moved data dir keeps the old
    dir's paths. Each analyzed one (export found its ANLZ through rekordbox)
    must still have its live rekordbox link, and its live djmdContent row must
    still have the FolderPath and AnalysisDataPath the import left, as the
    completion marker records them (the rekordbox adapter resolves the audio and
    the ANLZ through them). The recorded value,
    not (private path), is the reference: the pack's own importer decides the
    path (the first pack's matched rows by path and left some unrewritten), so
    the check catches what changed since the import, and a marker without the
    record means importing again. A full integrity check reads every
    page (85 s for the first pack's 1.7 GB state.db), too slow for the warm path;
    these queries take milliseconds there. An emptied or unrelated database lacks
    the tables, a foreign file does not open, and SQLite itself refuses a file
    shorter than the page count its header records. Damage confined to pages
    these queries never read is not seen here.
    """
    state, master = "state/state.db", "master.plain.db"
    ids = list(bundled)
    want = {sid: str((data / bundle).resolve()) for sid, bundle in bundled.items()}
    marks = ",".join("?" * len(ids))
    try:
        if state not in present:
            return []
        con = sqlite3.connect(f"{(data / state).resolve().as_uri()}?mode=ro", uri=True)
        try:
            paths = dict(
                con.execute(
                    f"SELECT stable_id, file_path FROM tracks WHERE deleted_at IS NULL AND stable_id IN ({marks})", ids
                ).fetchall()
            )
            tables = {r for (r,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            # As the importer reads it: no vendor table means no rekordbox links.
            linked = _rekordbox_links(con, ids) if "track_vendor_ids" in tables else {}
        finally:
            con.close()
        if paths != want or not analyzed <= set(linked):
            return [state]
        # The app's audio picker weighs every location row beside tracks.file_path,
        # so a row added or repointed since the import could play other bytes.
        if locations is None or _location_rows(data / state, bundled, data) != locations:
            return [state]
    except (OSError, sqlite3.Error):
        return [state]
    if master not in present:
        return []
    # Only tracks whose ANLZ the pack carries need their row: an unanalyzed
    # track (adopt --allow-unanalyzed) may be linked to a row that is not there,
    # and no import can create it.
    found = _master_folder_paths(data / master, {linked[sid] for sid in analyzed if sid in linked})
    return [] if found is not None and found == recorded else [master]


def _rekordbox_links(con: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    """Live rekordbox id of each of these tracks that has one, as the importer reads state.db."""
    marks = ",".join("?" * len(ids)) or "NULL"
    return dict(
        con.execute(
            "SELECT stable_id, CAST(vendor_id AS TEXT) FROM track_vendor_ids WHERE vendor = 'rekordbox'"
            f" AND deleted_at IS NULL AND stable_id IN ({marks})",
            ids,
        ).fetchall()
    )


# The track_locations columns apps/shared/state/locations.py selects and orders
# candidates by: whose row it is, whether it is live, local or remote, which
# file or URL, and the order (available, role, created_at, location_id).
_PICKER_COLUMNS = (
    "location_id",
    "machine_id",
    "deleted_at",
    "kind",
    "role",
    "available",
    "created_at",
    "file_path",
    "remote_url",
)


def _location_rows(state_db: Path, bundled: dict[str, str], data: Path) -> list[list[str | None]] | None:
    """The track_locations rows that could lead the app to other audio than a pack track's own unpacked file.

    A track whose rows all name its unpacked file plays those bytes in any order
    and whoever owns the rows (the app upserts its own primary row), so its rows
    are left out. Every row of a track with any other row is recorded with all
    the columns the picker reads, since then ownership, liveness and order decide
    which file plays. The current importer deletes such rows, so for its packs this
    is empty. Sorted lists of strings, so the record survives JSON as compared;
    None if state.db does not open.
    """
    want = {sid: str((data / bundle).resolve()) for sid, bundle in bundled.items()}
    ids = sorted(want)
    marks = ",".join("?" * len(ids)) or "NULL"
    try:
        con = sqlite3.connect(f"{state_db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            tables = {r for (r,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "track_locations" not in tables:
                return []
            have = {r[1] for r in con.execute("PRAGMA table_info(track_locations)")}
            fields = ", ".join(c if c in have else "NULL" for c in _PICKER_COLUMNS)
            rows = con.execute(
                f"SELECT stable_id, {fields} FROM track_locations WHERE stable_id IN ({marks})", ids
            ).fetchall()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return None
    path_at = 1 + _PICKER_COLUMNS.index("file_path")
    elsewhere = {str(r[0]) for r in rows if r[path_at] is None or str(r[path_at]) != want[str(r[0])]}
    kept = [[str(v) if v is not None else None for v in r] for r in rows if str(r[0]) in elsewhere]
    return sorted(kept, key=lambda r: [(v is not None, v or "") for v in r])


def _master_record(zpath: Path, data: Path, unanalyzed: set[str]) -> list[list[str | None]] | None:
    """What the completion marker records of master.plain.db: the analyzed tracks' rows as the import left them."""
    with zipfile.ZipFile(zpath) as z:
        manifest = json.loads(z.read("manifest.json"))
    if not manifest.get("has_master_plain_db"):
        return None
    analyzed = sorted(str(sid) for sid, r in manifest["tracks"].items() if r.get("anlz") and str(sid) not in unanalyzed)
    try:
        con = sqlite3.connect(f"{(data / 'state' / 'state.db').resolve().as_uri()}?mode=ro", uri=True)
        try:
            tables = {r for (r,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            linked = _rekordbox_links(con, analyzed) if "track_vendor_ids" in tables else {}
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return None  # nothing to compare against: the next ensure imports again
    return _master_folder_paths(data / "master.plain.db", set(linked.values()))


def _master_folder_paths(master: Path, wanted: set[str]) -> list[list[str | None]] | None:
    """Sorted [id, FolderPath, AnalysisDataPath] of the wanted rekordbox ids' live djmdContent rows, or None if the database does not open.

    The rekordbox adapter finds the audio through FolderPath and the ANLZ through
    AnalysisDataPath. An id with no live row is left out, so a lost row changes
    the result too. Lists, not tuples, so the record survives JSON as it is compared.
    """
    try:
        con = sqlite3.connect(f"{master.resolve().as_uri()}?mode=ro", uri=True)
        try:
            ids = sorted(wanted)
            rb = ",".join("?" * len(ids)) or "NULL"
            # Live as the rekordbox adapter reads it (rb_local_deleted = 0), where the column exists.
            cols = {r[1] for r in con.execute("PRAGMA table_info(djmdContent)")}
            live = " AND rb_local_deleted = 0" if "rb_local_deleted" in cols else ""
            fields = ", ".join(c if c in cols else "NULL" for c in ("FolderPath", "AnalysisDataPath"))
            rows = con.execute(
                f"SELECT CAST(ID AS TEXT), {fields} FROM djmdContent WHERE CAST(ID AS TEXT) IN ({rb}){live}", ids
            ).fetchall()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return None
    return sorted(
        ([str(v) if v is not None else None for v in r] for r in rows),
        key=lambda r: [(v is not None, v or "") for v in r],
    )


def _put_back(aside: Path, data: Path, importing: Path) -> bool:
    """Move a library moved aside back into data, replacing whatever an import left there.

    Once every root was moved (the aside holds MOVED) and the importing marker
    was written, the import may have run and created a root the earlier library
    did not have, so all of them are cleared first. Without the marker the import
    never started, and a run killed mid-move never imported: any root in data is
    then not import output and is never removed. If one of those collides with an
    entry the aside would put back, nothing is touched and False is returned, so
    the caller refuses rather than choose between two libraries.

    A library a fixture pack wrote (the aside holds OWNED) stays the script's: the
    importing marker is kept. One that --force alone let this script replace is the
    user's again, so the marker goes, and it goes while the aside still exists, so a
    run killed at any point here leaves either the aside to redo this or neither.
    """
    imported = (aside / MOVED).exists() and importing.exists()
    back = [e for e in aside.iterdir() if e.name not in (OWNED, MOVED)]
    if not imported and any(os.path.lexists(data / e.name) for e in back):
        return False
    if imported:
        for root in ("state", "audio", SHARE_PREFIX.rstrip("/"), "master.plain.db"):
            _remove_tree(data / root)
    (aside / MOVED).unlink(missing_ok=True)
    owned = (aside / OWNED).exists()
    for old in aside.iterdir():
        if old.name != OWNED:
            old.rename(data / old.name)
    if not owned:
        importing.unlink(missing_ok=True)
    # OWNED leaves with the aside in one rename, never on its own: a run killed
    # before this still finds it, and one killed after leaves a discard dir.
    discard = data / f"{DISCARD_PREFIX}{os.getpid()}"
    aside.rename(discard)
    _remove_tree(discard)
    return True


def _remove_tree(path: Path) -> None:
    """Remove path whatever it is: a directory tree, a file, or a link (never followed)."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _crc32(f: Path) -> int:
    crc = 0
    with open(f, "rb") as fh:
        while chunk := fh.read(1 << 20):
            crc = zlib.crc32(chunk, crc)
    return crc
