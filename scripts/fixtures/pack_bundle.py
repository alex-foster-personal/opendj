from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

try:
    from scripts.fixtures.pack_common import (
        IMPORTER_CANDIDATES,
        PREFIX,
        REPO,
        SHARE_PREFIX,
        TAG_RE,
        ZIP_ASSET,
        _extract_to,
        _gh,
        _log,
        _sha256,
    )
except ImportError:
    from pack_common import (
        IMPORTER_CANDIDATES,
        PREFIX,
        REPO,
        SHARE_PREFIX,
        TAG_RE,
        ZIP_ASSET,
        _extract_to,
        _gh,
        _log,
        _sha256,
    )


def _library(z: zipfile.ZipFile, tracks: dict) -> tuple[set[str], set[str]]:
    """(manifest ids the bundled state.db lists, ids whose ANLZ the bundled rekordbox db points at).

    The second set mirrors how the backend finds a track's analysis: its rekordbox
    vendor id in state.db, that ID's djmdContent.AnalysisDataPath in
    master.plain.db, then that path under pioneer-share. A claim that path does not
    reach is analysis the app never reads.
    """
    sids = list(tracks)
    marks = ",".join("?" * len(sids))
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "state.db"
        _extract_to(z, "state/state.db", db)
        con = sqlite3.connect(db)
        try:
            listed = {
                r[0]
                for r in con.execute(
                    f"SELECT stable_id FROM tracks WHERE deleted_at IS NULL AND stable_id IN ({marks})", sids
                )
            }
            vendor = dict(
                con.execute(
                    f"SELECT stable_id, vendor_id FROM track_vendor_ids WHERE vendor='rekordbox' AND deleted_at IS NULL"
                    f" AND stable_id IN ({marks})",
                    sids,
                ).fetchall()
            )
        except sqlite3.DatabaseError as e:
            raise ValueError(f"{z.filename}: state/state.db is not a readable library ({e})") from e
        finally:
            con.close()
        linked: set[str] = set()
        if not vendor or "master.plain.db" not in z.namelist():
            return listed, linked
        plain = Path(tmp) / "master.plain.db"
        _extract_to(z, "master.plain.db", plain)
        pcon = sqlite3.connect(plain)
        try:
            for sid, vid in vendor.items():
                row = pcon.execute(
                    "SELECT AnalysisDataPath FROM djmdContent WHERE ID=? AND rb_local_deleted=0", (str(vid),)
                ).fetchone()
                if not row or not isinstance(row[0], str) or not row[0].startswith("/PIONEER/"):
                    continue
                dat = SHARE_PREFIX + row[0].lstrip("/")
                claims = tracks[sid].get("anlz") or []
                if dat in claims and all(c.rsplit("/", 1)[0] == dat.rsplit("/", 1)[0] for c in claims):
                    linked.add(sid)
        except sqlite3.DatabaseError as e:
            raise ValueError(f"{z.filename}: master.plain.db is not a readable rekordbox db ({e})") from e
        finally:
            pcon.close()
    return listed, linked


def _tag_ok(tag: str) -> bool:
    """A real UTC timestamp, not ahead of now by more than a day.

    Newest wins by string order, so an impossible or far-future stamp would sort
    after every later pack and pin sessions to it.
    """
    if not TAG_RE.fullmatch(tag):
        return False
    try:
        when = datetime.strptime(tag[len(PREFIX) :], "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return False
    return when <= datetime.now(UTC) + timedelta(days=1)


def _check_tag(tag: str) -> None:
    if not _tag_ok(tag):
        raise ValueError(
            f"tag {tag!r} must be {PREFIX}YYYYMMDD-HHMMSS for a real UTC time no later than "
            "tomorrow, or newest-wins resolution would misorder it"
        )


def _unplayable_audio(zpath: Path, tracks: dict) -> dict[str, str]:
    """track id -> reason, for each bundled audio file the app's own playable-audio probe refuses."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from apps.shared.audio_playable import UnplayableAudioError, probe_playable_audio

    bad: dict[str, str] = {}
    with zipfile.ZipFile(zpath) as z, tempfile.TemporaryDirectory() as tmp:
        for sid, row in tracks.items():
            dest = Path(tmp) / ("audio" + PurePosixPath(row["bundle_path"]).suffix)  # the probe reads the extension
            with z.open(row["bundle_path"]) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            try:
                probe_playable_audio(dest)
            except UnplayableAudioError as e:
                bad[sid] = e.reason
            dest.unlink()
    return bad


def _unreachable_analysis(manifest: dict, names: set[str], linked: set[str]) -> list[str]:
    """Track ids whose bundled analysis the app cannot reach: pack.json's "unanalyzed".

    A claim counts only when it names a bundled raw ANLZ file the importer places
    under pioneer-share; any other member (state/state.db, say) proves nothing.
    """
    bundled = {n for n in manifest.get("anlz_files", {}) if n.startswith(SHARE_PREFIX) and n in names}
    return sorted(
        sid
        for sid, row in manifest["tracks"].items()
        if not row.get("anlz") or not set(row["anlz"]) <= bundled or sid not in linked
    )


def _meta(zpath: Path, importer: Path, tag: str, note: str, allow_unanalyzed: bool = False) -> dict:
    """pack.json for a zip; refuses one whose tracks lack their raw ANLZ files.

    A zip built elsewhere (an older export, or one made with the test-only
    --allow-unanalyzed) skips cmd_export's check, so the manifest is checked here.
    """
    try:
        with zipfile.ZipFile(zpath) as z:
            manifest = json.loads(z.read("manifest.json"))
            if not isinstance(manifest, dict):
                raise ValueError(f"{zpath.name}: manifest.json is not an object")  # noqa: TRY004 -- callers treat ValueError as a refused bundle
            names = set(z.namelist())
            lib = _library(z, manifest.get("tracks", {})) if "state/state.db" in names else None
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError, sqlite3.DatabaseError) as e:
        raise ValueError(f"{zpath.name} is not a fixture bundle: {type(e).__name__}: {e}") from e
    # The importer refuses any other manifest version, so a pack it cannot read must never be registered.
    m = re.search(r"^MANIFEST_VERSION = (\d+)$", importer.read_text(encoding="utf-8"), re.M)
    if m is None or manifest.get("version") != int(m.group(1)):
        raise ValueError(
            f"{zpath.name} has manifest version {manifest.get('version')!r}; "
            f"{importer.name} reads {m.group(1) if m else 'no declared version'}"
        )
    if not manifest.get("tracks"):
        raise ValueError(f"{zpath.name} bundles no tracks")
    # The importer points each manifest id's tracks row at its bundled audio, so an
    # id the bundled state.db lacks would hydrate as a track the library never lists.
    if lib is None:
        raise ValueError(f"{zpath.name} bundles no state/state.db")
    listed, linked = lib
    if manifest.get("has_master_plain_db") and "master.plain.db" not in names:
        raise ValueError(f"{zpath.name}: manifest declares master.plain.db but the zip lacks it")
    unbundled = sorted(sid for sid, row in manifest["tracks"].items() if row.get("bundle_path") not in names)
    if unbundled:
        raise ValueError(f"{zpath.name}: audio for {unbundled} is not in the zip")
    absent = sorted(set(manifest["tracks"]) - listed)
    if absent:
        raise ValueError(f"{zpath.name}: state/state.db has no live tracks row for {absent}")
    bare = _unreachable_analysis(manifest, names, linked)
    if bare and not allow_unanalyzed:
        raise ValueError(
            f"{len(bare)} track(s) in {zpath.name} carry no rekordbox ANLZ files their "
            f"master.plain.db row points at: {bare}; re-export them"
        )
    # Last, the pack's own importer judges the bundle exactly as ensure will run it,
    # so a zip it would refuse (or wipe a library for, then fail) is never registered.
    r = subprocess.run(
        [sys.executable, "-I", str(importer), "check", "--zip", str(zpath)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if r.returncode != 0:
        raise ValueError(f"{importer.name} would not import {zpath.name}: {(r.stderr or r.stdout).strip()[:500]}")
    # Only once the importer has passed the bundle (CRCs included): each track's
    # audio must pass the app's own playable-audio probe, or ensure would advertise
    # a track the deck cannot decode.
    if unplayable := _unplayable_audio(zpath, manifest["tracks"]):
        raise ValueError(f"{zpath.name}: bundled audio the app would not play: {unplayable}")
    return {
        "tag": tag,
        "sha256": _sha256(zpath),
        "bytes": zpath.stat().st_size,
        "manifest_version": manifest.get("version"),
        "created_utc": manifest.get("created_utc"),
        "stable_ids": sorted(manifest["tracks"]),
        "state_files": len(manifest.get("state_files", [])),
        "importer_sha256": _sha256(importer),
        "unanalyzed": bare,
        "note": note,
    }


def _private_repo_problem(repo: str) -> str | None:
    """Why repo cannot receive a pack, or None when GitHub reports it private.

    A release uploads real audio and the whole library's metadata, which cannot
    be taken back, so anything but a confirmed private repo is refused.
    """
    try:
        info = json.loads(_gh(f"repos/{repo}"))
    except (RuntimeError, ValueError) as e:
        return f"cannot confirm {repo} is private ({e}); not publishing"
    if not isinstance(info, dict) or info.get("private") is not True:
        return f"{repo} is not a private repository (visibility {info.get('visibility') if isinstance(info, dict) else '?'}); not publishing"
    return None
