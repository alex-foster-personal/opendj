"""Pre-create BLANK rekordbox playlists for an ingest batch (gated live write).

Mapped in ``apps.shared.rekordbox_writeback.WRITE_SURFACES`` as
``module.rekordbox_ingestion.create_blank_playlists``. Rails, in order, all
fail-fast:

1. the one-way gate (``MDT_REKORDBOX_WRITEBACK_ENABLED=1`` or refuse);
2. rekordbox closed: no process of the rekordbox bundle (exact, case-sensitive
   names) AND no process holding master.db / -wal / -shm open (lsof);
3. a timestamped backup of the WHOLE DB set (master.db, -wal, -shm,
   masterPlaylists6.xml) with sha256 checked against the source;
4. ``PRAGMA integrity_check`` before the write;
5. write through pyrekordbox's own ``create_playlist_folder`` /
   ``create_playlist`` (they keep the USN registry rekordbox uses for change
   tracking), then commit and close. pyrekordbox's own masterPlaylists6.xml
   sync is DETACHED because it re-stamps every existing node; instead the new
   nodes are appended to the XML text, in the file's own newline and indent;
6. re-open and verify: every playlist exists under the folder, is a plain
   playlist (Attribute 0), the DB passes integrity_check, each new ID is in
   masterPlaylists6.xml, and no pre-existing XML node changed.

Only creates. Never renames, empties, or deletes anything. An existing playlist
of the same name under the folder is reported and left untouched.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from apps.rekordbox_ingestion.config import CFG
from apps.shared.hashing import sha256_file_hex
from apps.shared.rekordbox_db import is_plain_sqlite
from apps.shared.rekordbox_writeback import require_writeback_enabled


class RekordboxNotClosed(RuntimeError):
    """A rekordbox process, or any process, still has the DB set open."""


class VerificationFailed(RuntimeError):
    """The post-write re-read did not show what was written."""


@dataclass(frozen=True)
class BackupSet:
    directory: Path
    files: tuple[Path, ...]


@dataclass
class PlaylistPlan:
    folder: str
    folder_exists: bool
    to_create: list[str]
    already_present: dict[str, int] = field(default_factory=dict)


# ----- rail 2: rekordbox closed ------------------------------------------------


def running_rekordbox_processes() -> list[str]:
    """``name(pid)`` for every live process of the rekordbox bundle."""
    found: list[str] = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = proc.info.get("name") or ""
        if name in CFG.REKORDBOX_PROCESS_NAMES:
            found.append(f"{name}({proc.info['pid']})")
    return found


def db_set_holders(db_path: Path) -> list[str]:
    """Processes with any file of the DB set open, as ``name(pid)``. Raises if it can't answer.

    macOS and Linux ask lsof; Windows has no lsof, so it reads each process's open
    files through psutil. Either way a probe that cannot answer refuses (fail closed).
    """
    if not Path(db_path).is_file():
        raise FileNotFoundError(f"rekordbox DB not found: {db_path}")
    # Never call lsof with an empty file list: it then lists EVERY open file.
    targets = [str(p) for p in _db_set_paths(db_path)]
    if sys.platform == "win32":
        return _db_set_holders_via_psutil(targets)
    lsof = shutil.which("lsof")
    if lsof is None:
        raise RekordboxNotClosed("lsof is not installed, so nothing can prove the DB set is closed")
    result = subprocess.run(
        [lsof, "-w", "-F", "pc", "--", *targets], capture_output=True, text=True, check=False
    )
    # lsof exits 1 with empty stdout when nobody has the files open. Anything
    # else with no output is an lsof failure, which must not read as "closed".
    if result.returncode not in (0, 1) or (result.returncode == 1 and result.stderr.strip()):
        raise RekordboxNotClosed(f"lsof could not answer (rc={result.returncode}): {result.stderr.strip()}")
    holders: list[str] = []
    pid = ""
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("c"):
            holders.append(f"{line[1:]}({pid})")
    return holders


def _db_set_holders_via_psutil(targets: list[str]) -> list[str]:
    """Windows holder probe: every process of this user with a target file open.

    Another user's processes deny the read (AccessDenied) and are passed over: they
    cannot be this user's rekordbox, which running_rekordbox_processes() checks by name.
    """
    wanted = {os.path.normcase(os.path.abspath(t)) for t in targets}
    holders: list[str] = []
    for proc in psutil.process_iter(["name"]):
        try:
            open_paths = {os.path.normcase(f.path) for f in proc.open_files()}
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        if open_paths & wanted:
            holders.append(f"{proc.info['name']}({proc.pid})")
    return holders


def assert_rekordbox_closed(db_path: Path) -> None:
    processes = running_rekordbox_processes()
    holders = db_set_holders(db_path)
    if processes or holders:
        raise RekordboxNotClosed(
            f"refusing to write {db_path}: rekordbox processes={processes} "
            f"db holders={holders}. Quit rekordbox (Cmd+Q) and retry."
        )


# ----- rail 3: backup ------------------------------------------------------------


def _db_set_paths(db_path: Path) -> list[Path]:
    paths = [Path(f"{db_path}{suffix}") for suffix in CFG.DB_SET_SUFFIXES]
    return [p for p in paths if p.exists()]


def backup_db_set(db_path: Path, backup_root: Path) -> BackupSet:
    """Copy master.db + -wal + -shm + masterPlaylists6.xml; verify each by sha256."""
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = backup_root / f"{stamp}-rekordbox-ingestion"
    directory.mkdir(parents=True, exist_ok=False)
    sources = _db_set_paths(db_path)
    xml = db_path.parent / CFG.PLAYLIST_XML_NAME
    if xml.exists():
        sources.append(xml)
    if Path(db_path) not in sources:
        raise FileNotFoundError(f"rekordbox DB not found: {db_path}")
    copied: list[Path] = []
    manifest: list[str] = []
    for source in sources:
        destination = directory / source.name
        shutil.copy2(source, destination)
        source_hash, copy_hash = sha256_file_hex(source), sha256_file_hex(destination)
        if source_hash != copy_hash:
            raise RuntimeError(f"backup copy differs from source: {source} -> {destination}")
        copied.append(destination)
        manifest.append(f"{copy_hash}  {source.name}")
    # Standard `shasum -a 256 -c` format, so the restore step can check it as-is.
    (directory / "MANIFEST.sha256").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    (directory / "SOURCE_DIR.txt").write_text(f"{db_path.parent}\n", encoding="utf-8")
    return BackupSet(directory=directory, files=tuple(copied))


# ----- plan / write / verify -----------------------------------------------------


def _open(db_path: Path):
    from pyrekordbox import Rekordbox6Database

    return Rekordbox6Database(path=str(db_path), unlock=not is_plain_sqlite(db_path))


def _close(db) -> None:
    """close() leaves the SQLAlchemy pool holding the file open; dispose releases it."""
    db.close()
    db.engine.dispose()


def _integrity(db) -> str:
    from sqlalchemy import text

    return str(db.session.execute(text("PRAGMA integrity_check")).scalar())


def _find_folder(db, folder: str):
    matches = [p for p in db.get_playlist(Name=folder) if p.Attribute == 1 and p.ParentID == "root"]
    if len(matches) > 1:
        raise RuntimeError(f"{len(matches)} root folders named {folder!r}; refusing to guess")
    return matches[0] if matches else None


def _children(db, folder_row) -> dict[str, Any]:
    return {p.Name: p for p in db.get_playlist(ParentID=folder_row.ID)} if folder_row else {}


def plan_blank_playlists(db_path: Path, folder: str, names: list[str]) -> PlaylistPlan:
    """Read-only: what a write would create. Reads the DB directly; rekordbox must be closed."""
    if not names:
        raise ValueError("no playlist names given")
    db = _open(db_path)
    try:
        folder_row = _find_folder(db, folder)
        children = _children(db, folder_row)
        present = {n: len(list(children[n].Songs)) for n in names if n in children}
        return PlaylistPlan(
            folder=folder, folder_exists=folder_row is not None,
            to_create=[n for n in names if n not in children], already_present=present,
        )
    finally:
        _close(db)


def verify_playlists(db_path: Path, folder: str, names: list[str]) -> dict[str, str]:
    """Re-open and prove each playlist exists under the folder; returns name -> ID."""
    from pyrekordbox.db6.database import MasterPlaylistXml

    db = _open(db_path)
    try:
        integrity = _integrity(db)
        if integrity != "ok":
            raise VerificationFailed(f"integrity_check after write: {integrity!r}")
        folder_row = _find_folder(db, folder)
        if folder_row is None:
            raise VerificationFailed(f"folder {folder!r} absent after write")
        children = _children(db, folder_row)
        ids: dict[str, str] = {}
        for name in names:
            row = children.get(name)
            if row is None or row.Attribute != 0:
                raise VerificationFailed(f"playlist {name!r} missing or not a plain playlist after write")
            ids[name] = str(row.ID)
    finally:
        _close(db)
    xml_path = db_path.parent / CFG.PLAYLIST_XML_NAME
    if xml_path.exists():
        xml = MasterPlaylistXml(db_dir=db_path.parent)
        absent = [n for n, pid in ids.items() if xml.get(pid) is None]
        if absent or xml.get(str(folder_row.ID)) is None:
            raise VerificationFailed(f"{CFG.PLAYLIST_XML_NAME} lacks entries for {absent or [folder]}")
    return ids


def _write_playlists(db, folder: str, plan: PlaylistPlan) -> list[dict[str, str]]:
    """Create the folder (if absent) and the planned playlists, commit; return XML nodes."""
    integrity = _integrity(db)
    if integrity != "ok":
        raise RuntimeError(f"integrity_check BEFORE write failed: {integrity!r}; nothing written")
    # pyrekordbox's commit() re-stamps EVERY playlist's XML Timestamp from the
    # DB and re-serializes the file (CRLF -> LF): 143 of 145 nodes changed on
    # a copy, Thu 29 Oct 2026. Detach it so the DB write is the only effect;
    # the caller appends just our nodes to the XML text.
    db.playlist_xml = None
    new_nodes: list[dict[str, str]] = []
    folder_row = _find_folder(db, folder)
    if folder_row is None:
        folder_row = db.create_playlist_folder(folder)
        new_nodes.append(_xml_node(folder_row))
    existing = _children(db, folder_row)
    for name in plan.to_create:
        if name in existing:
            raise RuntimeError(f"{name!r} appeared between plan and write; aborting")
        new_nodes.append(_xml_node(db.create_playlist(name, parent=folder_row)))
    db.commit()
    return new_nodes


def create_blank_playlists(
    db_path: Path, folder: str, names: list[str], *, backup_root: Path = CFG.BACKUP_ROOT
) -> tuple[BackupSet | None, PlaylistPlan, dict[str, str]]:
    """The gated write. Returns the backup (None when nothing needed creating),
    the plan it executed, and name -> ID.

    ORDER MATTERS: opening and closing the live DB with pyrekordbox checkpoints
    the WAL into master.db and deletes -wal/-shm (the last connection's close).
    So nothing opens the live file until the backup exists; the no-op check and
    the no-op verify read a temp snapshot instead.
    """
    from apps.rekordbox_ingestion.status import snapshot_db

    require_writeback_enabled("module.rekordbox_ingestion.create_blank_playlists")
    assert_rekordbox_closed(db_path)
    with snapshot_db(db_path) as snapshot:
        preview = plan_blank_playlists(snapshot, folder, names)
        if not preview.to_create:
            return None, preview, verify_playlists(snapshot, folder, names)
    backup = backup_db_set(db_path, backup_root)
    plan = preview
    xml_path = db_path.parent / CFG.PLAYLIST_XML_NAME
    xml_before = _xml_nodes(xml_path) if xml_path.exists() else None
    db = _open(db_path)
    try:
        new_nodes = _write_playlists(db, folder, plan)
    except BaseException:
        db.rollback()
        raise
    finally:
        _close(db)
    if xml_before is not None and new_nodes:
        try:
            _append_xml_nodes(xml_path, new_nodes)
        except OSError as exc:
            raise RuntimeError(
                f"playlists are committed to master.db but {xml_path} was not updated; "
                f"restore from the backup at {backup}"
            ) from exc
    ids = verify_playlists(db_path, folder, names)
    if xml_before is not None:
        _assert_xml_only_grew(xml_before, _xml_nodes(xml_path), new_nodes)
    return backup, plan, ids


# ----- masterPlaylists6.xml: append-only text patch ------------------------------


def _xml_node(row) -> dict[str, str]:
    parent = str(row.ParentID)
    return {
        "Id": f"{int(row.ID):X}",
        "ParentId": "0" if parent == "root" else f"{int(parent):X}",
        "Attribute": str(int(row.Attribute)),
        "Timestamp": str(int(row.updated_at.timestamp() * 1000)),
        "Lib_Type": "0",
        "CheckType": "0",
    }


def _xml_nodes(xml_path: Path) -> dict[str, dict[str, str]]:
    import xml.etree.ElementTree as ET

    # masterPlaylists6.xml is Rekordbox's own file in its local data directory, never outside
    # input, and the stdlib parser resolves no external entities (same call as
    # scripts/ci_junit_failures.py).
    # nosemgrep: python.lang.security.use-defused-xml-parse.use-defused-xml-parse
    root = ET.parse(xml_path).getroot()
    return {n.get("Id", ""): dict(n.attrib) for n in root.iter("NODE")}


def _append_xml_nodes(xml_path: Path, nodes: list[dict[str, str]]) -> None:
    """Insert NODE lines before </PLAYLISTS>, keeping the file's own newline and indent."""
    raw = xml_path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    anchor = raw.rfind("</PLAYLISTS>")
    if anchor < 0:
        raise RuntimeError(f"{xml_path} has no </PLAYLISTS>; refusing to patch")
    line_start = raw.rfind(newline, 0, anchor) + len(newline)
    closing_indent = raw[line_start:anchor]
    lines = "".join(
        f"{closing_indent}  <NODE " + " ".join(f'{k}="{v}"' for k, v in node.items()) + f"/>{newline}"
        for node in nodes
    )
    tmp = xml_path.with_name(xml_path.name + ".rbx-ingest.tmp")
    tmp.write_bytes((raw[:line_start] + lines + raw[line_start:]).encode("utf-8"))
    shutil.copymode(xml_path, tmp)
    tmp.replace(xml_path)


def _assert_xml_only_grew(before: dict, after: dict, added: list[dict[str, str]]) -> None:
    added_ids = {n["Id"] for n in added}
    changed = [k for k in before if after.get(k) != before[k]]
    if changed or set(after) - set(before) != added_ids:
        raise VerificationFailed(
            f"{CFG.PLAYLIST_XML_NAME} changed beyond the append: changed={changed[:5]} "
            f"added={sorted(set(after) - set(before))} expected={sorted(added_ids)}"
        )
