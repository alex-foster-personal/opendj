"""Versioned fixture packs: fully analyzed tracks for cloud sessions, without the faff.

A pack is one zip made by the perf-lab ``fixture.py export`` (state.db and
master.plain.db copies, the bundled tracks' audio, their anlz-cache, waveform,
stem and lyric files, manifest.json). Packs live as assets on GitHub releases
tagged ``fixture-pack-*`` in the PRIVATE repo private_owner/odj-private,
next to the real-library tarballs already kept there. The audio and the whole
library's metadata are in every pack, so a pack never goes in a public repo,
an artifact, or an issue.

publish (Silver, or any machine with the real library and gh signed in):
  python scripts/fixtures/pack.py publish --data-dir <repo>/data --pick 6
  python scripts/fixtures/pack.py publish --data-dir <repo>/data --ids sid1,sid2,...
  python scripts/fixtures/pack.py publish --zip odj-fixture.zip   # a zip already exported
  Each publish is a new immutable version (tag fixture-pack-YYYYMMDD-HHMMSS unless
  --tag). The importer (fixture.py) is uploaded with the zip, so any later
  session can unpack the pack even on a branch that lacks the script.

ensure (any session; idempotent, cheap when already done):
  python scripts/fixtures/pack.py ensure            # newest pack
  python scripts/fixtures/pack.py ensure --tag fixture-pack-20261013-073000
  Finds the pack in a local cache, then the project's shared folder
  (/mnt/project-files/fixtures/packs), then GitHub; checks its sha256; unpacks it
  with the pack's own fixture.py into ~/.cache/odj-fixtures/<tag>/data. Prints the
  data dir on the last line (``--print-env`` prints ODJ_FIXTURE_* lines instead),
  so callers can ``DATA=$(python scripts/fixtures/pack.py ensure | tail -1)``.

list: the published packs, newest first.

GitHub access goes through ``gh api`` (REST only; cloud sessions refuse GraphQL),
so the session needs odj-private attached. When it is not, ``ensure`` still
works from the shared-folder cache that the first fetching session fills.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

REPO = os.environ.get("ODJ_FIXTURE_REPO", "private_owner/odj-private")
PREFIX = "fixture-pack-"
# The only tag form: newest-wins resolution compares tags as strings, so a tag
# outside this form could sort after every later pack and pin sessions to it.
TAG_RE = re.compile(re.escape(PREFIX) + r"\d{8}-\d{6}")
ZIP_ASSET = "odj-fixture.zip"
IMPORTER_ASSET = "fixture.py"
PATH_MAP = "path-map.json"  # the app's load_path_map file name under a data dir
META_ASSET = "pack.json"
ROOT = Path(__file__).resolve().parents[2]
CACHE = Path(os.environ.get("ODJ_FIXTURE_CACHE", Path.home() / ".cache" / "odj-fixtures"))
SHARED = Path(os.environ.get("ODJ_FIXTURE_SHARED", "/mnt/project-files/fixtures/packs"))
PROJECT_FILES = Path("/mnt/project-files")
IMPORTER_CANDIDATES = (
    ROOT / "scripts" / "fixtures" / "fixture.py",
    ROOT / "scripts" / "perf" / "lab" / "fixture.py",
)


def _log(msg: str) -> None:
    print(f"[fixture-pack] {msg}", file=sys.stderr)


def _gh(*args: str, stdout=None) -> str:
    gh = shutil.which("gh")
    if gh is None:
        raise RuntimeError("gh CLI not found on PATH")
    try:
        # gh writes UTF-8 whatever the locale, so it is decoded as such, never in the locale's encoding.
        r = subprocess.run([gh, "api", *args], stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except FileNotFoundError as e:
        raise RuntimeError("gh CLI not installed") from e
    if r.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args[:3])} failed: {r.stderr.decode('utf-8', 'replace')[:400]}")
    return r.stdout.decode("utf-8") if stdout is None else ""


def _extract_to(z: zipfile.ZipFile, name: str, dest: Path) -> None:
    """Copy one member to dest in chunks: a real pack's state.db is gigabytes, too big to hold in memory."""
    with z.open(name) as src, open(dest, "wb") as dst:
        shutil.copyfileobj(src, dst, 1 << 20)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _importer(explicit: str | None = None) -> Path:
    for c in (explicit, os.environ.get("ODJ_FIXTURE_PY"), *IMPORTER_CANDIDATES):
        if c and Path(c).exists():
            return Path(c)
    raise FileNotFoundError("fixture.py not found; pass --fixture-py or set ODJ_FIXTURE_PY")


def _releases() -> list[dict]:
    # One release object per line across every page, so old tags stay resolvable.
    out = [
        json.loads(line)
        for line in _gh(f"repos/{REPO}/releases?per_page=100", "--paginate", "--jq", ".[]").splitlines()
        if line
    ]
    required = {ZIP_ASSET, IMPORTER_ASSET, META_ASSET}
    packs = [
        r
        for r in out
        if _tag_ok(r["tag_name"])
        and not r["draft"]
        and required <= {x["name"] for x in r["assets"] if x.get("state", "uploaded") == "uploaded"}
    ]
    return sorted(packs, key=lambda r: r["created_at"], reverse=True)


DISCARD_PREFIX = ".fixture-pack.discard-"  # a library the import replaced, being deleted
# An aside being set up: it holds at most its OWNED marker, never a library, and is
# renamed into place whole so no run ever sees an aside without its ownership.
STARTING_PREFIX = ".fixture-pack.starting-"
MOVED = ".moved"  # in a library moved aside: every root was moved before the import began
OWNED = ".owned"  # in a library moved aside: a fixture pack wrote it, so the dir stays the script's
# Completion-marker key: the analyzed tracks' master.plain.db rows (id, FolderPath,
# AnalysisDataPath) as the import left them.
MASTER_RECORD = "master_folder_paths"
# Completion-marker key: the track_locations rows that could lead a pack track to
# audio other than its own unpacked file, as the import left them (see _location_rows).
LOCATION_RECORD = "track_location_rows"
# Members the import does not leave as the zip's bytes: the manifest stays in the
# zip, and fixture.py deletes any bundled WAL or SHM beside state.db.
NOT_EXTRACTED = ("manifest.json", "state/state.db-wal", "state/state.db-shm")
# Completion-marker key: the zip's _stamp when its sha256 last matched pack.json.
ZIP_STAMP = "zip_stamp"
SHARE_PREFIX = "pioneer-share/"  # fixture.py SHARE_ARC


def _upload(release: dict, path: Path, name: str) -> None:
    url = release["upload_url"].split("{", 1)[0] + f"?name={name}"
    _gh("--method", "POST", url, "-H", "Content-Type: application/octet-stream", "--input", str(path))


def _new_tag() -> str:
    return PREFIX + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


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


def cmd_publish(a: argparse.Namespace) -> int:
    importer = _importer(a.fixture_py)
    tag = a.tag or _new_tag()
    try:
        _check_tag(tag)
    except ValueError as e:
        _log(str(e))
        return 2
    if problem := _private_repo_problem(REPO):
        _log(problem)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        zpath = Path(tmp) / ZIP_ASSET
        if a.zip:
            shutil.copyfile(a.zip, zpath)
        else:
            cmd = [
                sys.executable,
                str(importer),
                "export",
                "--data-dir",
                a.data_dir,
                "--out",
                str(zpath),
                "--pick",
                str(a.pick),
                "--max-audio-mb",
                str(a.max_audio_mb),
            ]
            if a.ids:
                cmd += ["--ids", a.ids]
            if a.with_stems:
                cmd.append("--with-stems")
            if subprocess.run(cmd, check=False).returncode != 0:
                return 2
        size = zpath.stat().st_size
        if size >= 2 * 1024**3:
            _log(
                f"zip is {size / 1024**3:.2f} GiB; GitHub assets max out at 2 GiB. Use fewer tracks or drop --with-stems."
            )
            return 2
        try:
            meta = _meta(zpath, importer, tag, a.note)  # publish has no override
        except ValueError as e:
            _log(str(e))
            return 2
        (Path(tmp) / META_ASSET).write_text(json.dumps(meta, indent=1), encoding="utf-8", newline="\n")
        body = (f"{a.note}\n\n" if a.note else "") + (
            f"{len(meta['stable_ids'])} tracks, {meta['state_files']} state files, {meta['bytes'] / 1048576:.0f} MB.\n"
            "PRIVATE: real audio plus the whole library's metadata. Fetch with "
            "`python scripts/fixtures/pack.py ensure` in music-dj-tools.\n\n"
            f"```json\n{json.dumps(meta, indent=1)}\n```\n"
        )
        # Drafts are invisible to `ensure`, so a publish that dies mid-upload never
        # becomes the newest pack; it goes public only once all three assets are up.
        rel = json.loads(
            _gh(
                "--method",
                "POST",
                f"repos/{REPO}/releases",
                "-f",
                f"tag_name={tag}",
                "-f",
                f"name={tag}",
                "-f",
                f"body={body}",
                "-f",
                "make_latest=false",
                "-F",
                "draft=true",
            )
        )
        _upload(rel, zpath, ZIP_ASSET)
        _upload(rel, importer, IMPORTER_ASSET)
        _upload(rel, Path(tmp) / META_ASSET, META_ASSET)
        # Visibility can change during a long upload; recheck before the release goes live.
        if problem := _private_repo_problem(REPO):
            _log(f"{problem}; draft release {rel['id']} left unpublished, delete it")
            return 2
        rel = json.loads(_gh("--method", "PATCH", f"repos/{REPO}/releases/{rel['id']}", "-F", "draft=false"))
    print(f"[OK] {tag}: {len(meta['stable_ids'])} tracks, {meta['bytes'] / 1048576:.0f} MB -> {rel['html_url']}")
    return 0


def cmd_adopt(a: argparse.Namespace) -> int:
    """Turn a zip someone attached in the project into a shared-folder pack (no GitHub needed)."""
    importer = _importer(a.fixture_py)
    tag = a.tag or _new_tag()
    try:
        _check_tag(tag)
    except ValueError as e:
        _log(str(e))
        return 2
    dest = SHARED / tag
    if dest.exists():
        _log(f"{dest} already exists; pick another --tag")
        return 2
    dest.mkdir(parents=True)
    for src, n in ((Path(a.zip), ZIP_ASSET), (importer, IMPORTER_ASSET)):
        _copy_atomic(src, dest / n)
    try:
        meta = _meta(dest / ZIP_ASSET, importer, tag, a.note, a.allow_unanalyzed)
    except ValueError as e:
        shutil.rmtree(dest)
        _log(str(e))
        return 2
    except BaseException:
        shutil.rmtree(dest)  # never leave a half-adopted pack holding the tag
        raise
    _write_atomic(dest / META_ASSET, json.dumps(meta, indent=1))
    print(
        f"[OK] {tag}: {len(meta['stable_ids'])} tracks in {dest} (shared folder only; publish from Silver for a durable copy)"
    )
    return 0


def _part(dest: Path) -> Path:
    """A temp name no other process or container writes (the shared folder spans sessions)."""
    return dest.with_name(f"{dest.name}.{socket.gethostname()}.{os.getpid()}.part")


def _copy_atomic(src: Path, dest: Path) -> None:
    part = _part(dest)
    shutil.copyfile(src, part)
    part.replace(dest)


def _write_atomic(dest: Path, text: str) -> None:
    """A reader in another session sees the whole file or none of it."""
    part = _part(dest)
    part.write_text(text, encoding="utf-8", newline="\n")
    part.replace(dest)


def _fetch_asset(release: dict, name: str, dest: Path) -> None:
    asset = next((x for x in release["assets"] if x["name"] == name), None)
    if not asset:
        raise RuntimeError(f"{release['tag_name']} has no {name} asset")
    part = _part(dest)
    with open(part, "wb") as f:
        _gh("-H", "Accept: application/octet-stream", f"repos/{REPO}/releases/assets/{asset['id']}", stdout=f)
    part.replace(dest)


def _read_meta(d: Path) -> dict | None:
    """pack.json, or None if it is missing, truncated or lacks the hashes and ids ensure checks."""
    try:
        meta = json.loads((d / META_ASSET).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or not all(isinstance(meta.get(k), str) for k in ("sha256", "importer_sha256")):
        return None
    ids = meta.get("stable_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        return None
    # Every pack lives in a directory named by its tag. A pack.json naming another
    # tag is an old pack copied under a newer name, which newest-wins would then
    # advertise as the newer fixture.
    if meta.get("tag") != d.name:
        return None
    return meta


def _complete(d: Path) -> bool:
    return (d / IMPORTER_ASSET).exists() and (meta := _read_meta(d)) is not None and _whole_zip(d, meta)


def _whole_zip(d: Path, meta: dict) -> bool:
    """The zip is all there: as many bytes as pack.json records.

    The project folder is a network mount that can show a large file while its
    upload from another session is still in flight; a short one is neither used
    nor judged damaged, since reading it again later may well find it whole.
    """
    try:
        return (d / ZIP_ASSET).stat().st_size == meta.get("bytes")
    except OSError:
        return False


def _cached_tags() -> set[str]:
    return {
        p.name
        for base in (CACHE, SHARED)
        if base.is_dir()
        for p in base.glob(PREFIX + "*")
        if _tag_ok(p.name) and _complete(p)
    }


def _resolve(tag: str | None) -> tuple[str, Path]:
    """Return (tag, dir holding odj-fixture.zip + fixture.py + pack.json), fetching if needed.

    Candidates are GitHub releases plus packs already in the local or shared cache;
    without --tag the newest tag wins (tags sort by their UTC timestamp).
    """
    try:
        remote = {r["tag_name"]: r for r in _releases()}
    except RuntimeError as e:
        remote = {}
        _log(f"GitHub unavailable, using caches only: {str(e).splitlines()[0][:200]}")
    known = set(remote) | _cached_tags()
    if tag and tag not in known:
        raise RuntimeError(f"no pack {tag} on GitHub ({REPO}) or in {CACHE} / {SHARED}")
    if not tag:
        if not known:
            raise RuntimeError(
                f"no {PREFIX}* pack on GitHub ({REPO}) or in {CACHE} / {SHARED}; publish one from Silver"
            )
        tag = max(known)
    local = CACHE / tag
    for src in (local, SHARED / tag):
        if _complete(src):
            if src != local:
                local.mkdir(parents=True, exist_ok=True)
                for n in (META_ASSET, IMPORTER_ASSET, ZIP_ASSET):
                    # A truncated local pack.json is replaced, never kept.
                    if not (local / n).exists() or (n == META_ASSET and _read_meta(local) is None):
                        _copy_atomic(src / n, local / n)
            return tag, local
    rel = remote[tag]
    local.mkdir(parents=True, exist_ok=True)
    for n in (META_ASSET, IMPORTER_ASSET, ZIP_ASSET):
        _log(f"downloading {tag}/{n}")
        _fetch_asset(rel, n, local / n)
    fetched = _read_meta(local)
    if fetched is None:
        (local / META_ASSET).unlink(missing_ok=True)
        raise RuntimeError(f"downloaded {tag}/{META_ASSET} is unreadable; removed, rerun to refetch")
    want = fetched["sha256"]
    got = _sha256(local / ZIP_ASSET)
    if got != want:
        (local / ZIP_ASSET).unlink()
        raise RuntimeError(
            f"downloaded {tag} zip fails its sha256 ({got[:12]} != {want[:12]}); removed, rerun to refetch"
        )
    # The project mount is the signal, not fixtures/ under it: the first fetch in
    # a fresh project creates fixtures/packs so later sessions skip the download.
    if SHARED.parent.is_dir() or (SHARED.is_relative_to(PROJECT_FILES) and PROJECT_FILES.is_dir()):
        try:
            (SHARED / tag).mkdir(parents=True, exist_ok=True)
            for n in (IMPORTER_ASSET, ZIP_ASSET, META_ASSET):
                _copy_atomic(local / n, SHARED / tag / n)
            _log(f"cached in {SHARED / tag} for other sessions in this project")
        except OSError as e:
            _log(f"could not fill the shared cache: {e}")
    return tag, local


LOCK_STALE_S = 300
LOCK_BEAT_S = 30


class _Lock:
    """mkdir lock (atomic on every OS) with an owner token and a heartbeat.

    The holder touches the lock every LOCK_BEAT_S, so only a holder that died
    goes stale; release removes the lock only while the token is still ours.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.token = uuid.uuid4().hex
        self._stop = threading.Event()
        self._beat = threading.Thread(target=self._heartbeat, daemon=True)

    def _heartbeat(self) -> None:
        while not self._stop.wait(LOCK_BEAT_S):
            if self._owner() != self.token:
                return  # never keep someone else's lock fresh
            try:
                os.utime(self.path)
            except OSError:
                return

    def _stale(self, path: Path | None = None) -> bool:
        try:
            return time.time() - (path or self.path).stat().st_mtime > LOCK_STALE_S
        except FileNotFoundError:
            return False

    def _owner(self) -> str | None:
        try:
            return (self.path / "owner").read_text(encoding="utf-8")
        except OSError:
            return None

    def acquire(self, wait_s: float = 3600) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + wait_s
        while True:
            try:
                self.path.mkdir()
            except FileExistsError:
                if self._stale():
                    self._take_over()
                    continue
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"another ensure has held {self.path} too long; remove it if no ensure is running"
                    ) from None
                time.sleep(2)
            else:
                (self.path / "owner").write_text(self.token, encoding="utf-8", newline="\n")
                self._beat.start()
                return

    def _take_over(self) -> None:
        """Remove a stale lock, one contender at a time.

        Two processes can both see the same stale lock. Without the guard the
        slower one would delete the fresh lock the faster one just made, and
        both would import into the same paths. The staleness check is repeated
        while holding the guard, so the second contender sees the new lock and
        waits. A guard left by a contender that died is itself stale after
        LOCK_STALE_S.
        """
        guard = self.path.with_name(self.path.name + ".takeover")
        try:
            guard.mkdir()
        except FileExistsError:
            if self._stale(guard):
                shutil.rmtree(guard, ignore_errors=True)
            else:
                time.sleep(1)
            return
        try:
            if self._stale():
                _log(f"taking over a stale lock {self.path}")
                shutil.rmtree(self.path, ignore_errors=True)
        finally:
            shutil.rmtree(guard, ignore_errors=True)

    def release(self) -> None:
        self._stop.set()
        if self._owner() == self.token:
            shutil.rmtree(self.path, ignore_errors=True)


def cmd_ensure(a: argparse.Namespace) -> int:
    # A background prefetch and a foreground ensure must not download or import
    # into the same paths at once, so the whole ensure holds one lock.
    lock = _Lock(CACHE / ".ensure.lock")
    try:
        lock.acquire()
    except RuntimeError as e:
        _log(str(e))
        return 3
    try:
        return _ensure(a)
    finally:
        lock.release()


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


def _ensure(a: argparse.Namespace) -> int:
    try:
        tag, d = _resolve(a.tag)
    except RuntimeError as e:
        _log(str(e))
        return 3
    meta = _read_meta(d)
    assert meta is not None  # _resolve returns only complete packs
    # Resolved: the path is published (env, the `current` link) and used from other directories.
    data = Path(a.data_dir).expanduser().resolve() if a.data_dir else (d / "data").resolve()
    done = data / ".fixture-pack.json"
    _sweep_leftovers(data, done, a.force)
    stale = _done_id(done) != _pack_id(meta)
    if not stale and not _zip_still_verified(d / ZIP_ASSET, done, meta):
        # The warm checks below read the zip's manifest and CRCs, so they are only
        # as good as the zip: a replaced one would vouch for replaced files.
        _log(f"{d / ZIP_ASSET} is not the zip pack.json describes; importing it again")
        stale = True
    if not stale and (
        missing := _hydration_gaps(
            d / ZIP_ASSET,
            data,
            _done_record(done),
            set(meta.get("unanalyzed") or ()),
            _done_record(done, LOCATION_RECORD),
        )
    ):
        _log(f"{data} is missing or altered {missing[:5]} since {tag} was unpacked; importing it again")
        stale = True
    if stale:
        # Unpacking runs the importer with --force, which removes state/, audio/,
        # pioneer-share/ and master.plain.db. Only a dir this script filled before
        # (it has the marker) or one holding none of those may be unpacked into
        # without --force, so a mistyped --data-dir can never wipe anything.
        importing = data / ".fixture-pack.importing"
        # A run killed after moving a library aside left it there: it is the only
        # copy, so it goes back before anything below judges or replaces the dir.
        for left in sorted(data.glob(".fixture-pack.old-*")):
            # An unowned library: that run's --force was its consent, not this one's.
            if not _put_back(left, data, importing):
                _log(
                    f"{left} holds a library an interrupted run moved aside, and {data} has another in its"
                    " place; refusing to choose between them (move one away and rerun)"
                )
                return 2
            _log(f"put back the library an interrupted run had moved aside in {data}")
        ours = done.exists() or importing.exists()
        held = [n for n in ("state", "audio", SHARE_PREFIX.rstrip("/"), "master.plain.db") if os.path.lexists(data / n)]
        # The path map is checked here too, before anything is moved aside: the
        # entry is added after the import, which by then has replaced the library.
        refusal = (
            (
                f"{data} holds {held} that no fixture pack wrote; refusing to overwrite them (pass --force if they"
                " are disposable)"
            )
            if held and not ours and not a.force
            else _merged_map(data)[3]
        )
        if refusal:
            _log(refusal)
            return 2
        verified = _stamp(d / ZIP_ASSET)  # taken before the hash, so a change during it re-hashes next time
        got = _sha256(d / ZIP_ASSET)
        if got != meta["sha256"]:
            _drop_mismatched(tag, d)
            _log(f"sha256 mismatch for {tag} ({got[:12]} != {meta['sha256'][:12]}); removed, rerun to refetch")
            return 4
        # Only the pack's own importer runs, and only after it matches pack.json.
        importer = d / IMPORTER_ASSET
        if _sha256(importer) != meta["importer_sha256"]:
            _drop_mismatched(tag, d)
            _log(f"{tag}/{IMPORTER_ASSET} does not match pack.json; removed it rather than run it, rerun to refetch")
            return 4
        # pack.json's ids are what ensure advertises; they must be the zip's own.
        if not _describes_zip(meta, d / ZIP_ASSET):
            _drop_mismatched(tag, d)
            _log(
                f"{tag}/pack.json describes different tracks, version or unanalyzed tracks than its zip; removed it, rerun to refetch"
            )
            return 4
        data.mkdir(parents=True, exist_ok=True)
        if ours:  # still the script's whatever happens below, even if the import dies midway
            importing.write_text(tag, encoding="utf-8", newline="\n")
        # A dir an earlier pack filled may hold that pack's audio and state files
        # (stems, lyrics, anlz) that this pack lacks, and so may a library --force
        # replaces; only this pack's files may remain. They are moved aside rather
        # than deleted, because the importer checks the target itself (path limits,
        # SQLite) and a refusal there must leave the old library where it was.
        done.unlink(missing_ok=True)
        aside = data / f".fixture-pack.old-{os.getpid()}"
        if ours or a.force:
            starting = data / f"{STARTING_PREFIX}{os.getpid()}"
            _remove_tree(starting)
            starting.mkdir()
            if ours:
                (starting / OWNED).touch()
            starting.rename(aside)  # appears with its ownership already inside
            for owned in held:
                (data / owned).rename(aside / owned)
            (aside / MOVED).touch()  # every root is aside; from here on the import may write
        if not ours:
            # Only now: a run killed before this leaves no marker, so the next one
            # does not take a user's library for the script's (its aside, if any,
            # lacks OWNED and goes back as the user's).
            importing.write_text(tag, encoding="utf-8", newline="\n")
        r = subprocess.run(
            [
                sys.executable,
                "-I",
                str(importer),
                "import",
                "--zip",
                str(d / ZIP_ASSET),
                "--data-dir",
                str(data),
                "--force",
            ],
            stdout=sys.stderr,
            check=False,
        )
        if r.returncode != 0:
            if aside.exists():
                # An unowned library is the user's again: --force consented once.
                # importing exists here, so whatever the import wrote is cleared first.
                put = _put_back(aside, data, importing)
                assert put, "importing was written before the import ran"
                _log(f"the import into {data} failed; its earlier library was put back")
            return r.returncode
        if aside.exists():
            # Renamed first, in one step: a run killed mid-delete leaves a discard dir,
            # which the next run removes, never a partial aside it would put back.
            discard = data / f"{DISCARD_PREFIX}{os.getpid()}"
            aside.rename(discard)
            _remove_tree(discard)
        record = _master_record(d / ZIP_ASSET, data, set(meta.get("unanalyzed") or ()))
        with zipfile.ZipFile(d / ZIP_ASSET) as z:
            bundled = {
                str(sid): str(r["bundle_path"]) for sid, r in json.loads(z.read("manifest.json"))["tracks"].items()
            }
        locations = _location_rows(data / "state" / "state.db", bundled, data)
        _write_atomic(
            done, json.dumps({**meta, MASTER_RECORD: record, LOCATION_RECORD: locations, ZIP_STAMP: verified}, indent=1)
        )
        importing.unlink()
    return _serve(a, tag, data, meta)


def _sweep_leftovers(data: Path, done: Path, force: bool) -> None:
    """Remove what a killed run left under this script's own prefixes, in a dir it may write.

    A discard dir exists only after an import began here, so the completion or
    importing marker is there with it; a starting dir is made for an owned dir, or
    for one --force let it take. Anywhere else a name with the prefix is not the
    script's to delete, so without a marker or --force nothing is touched.
    """
    if not (done.exists() or (data / ".fixture-pack.importing").exists() or force):
        return
    for left in data.glob(f"{DISCARD_PREFIX}*"):
        _remove_tree(left)  # a successful import's leftover, killed while deleting it
    for left in data.glob(f"{STARTING_PREFIX}*"):
        _remove_tree(left)  # an aside killed before it was renamed into place: no library in it


def _point_current(data: Path) -> str | None:
    """Record data as the current pack dir, or say why not.

    CACHE/current.txt holds the path on every platform and must be written. The
    CACHE/current link is a convenience only a link may hold: a file or directory
    named current is someone else's and stops the run, untouched. Making the link
    can fail where links need a privilege (Windows without Developer Mode); then an
    earlier link is removed rather than left naming another dir, and a warning says so.
    """
    current = CACHE / "current"
    if os.path.lexists(current) and not current.is_symlink():
        return f"{current} is not a link, so it was left alone; move it away and rerun"
    try:
        _write_atomic(CACHE / "current.txt", f"{data}\n")
    except OSError as e:
        return f"{CACHE / 'current.txt'} could not be written ({e})"
    staged = CACHE / f".current-{os.getpid()}"
    try:
        staged.unlink(missing_ok=True)
        staged.symlink_to(data, target_is_directory=True)
        os.replace(staged, current)  # current is never missing while it moves
    except OSError as e:
        staged.unlink(missing_ok=True)
        current.unlink(missing_ok=True)  # a link, checked above: absent beats naming another dir
        _log(f"warning: {current} could not be made a link to {data} ({e}); {CACHE / 'current.txt'} names it")
        return None
    if Path(os.readlink(current)) != data:
        return f"{current} points at {os.readlink(current)} after updating it, not at {data}"
    return None


def _serve(a: argparse.Namespace, tag: str, data: Path, meta: dict) -> int:
    """Map the unpacked library for remote mode, then point `current` at it and print how to serve it."""
    try:
        _map_data_dir(data)
    except RuntimeError as e:
        _log(f"{tag} is unpacked in {data}, but remote mode cannot play it there: {e}")
        return 2
    problem = _point_current(data)
    if problem:
        _log(f"{tag} is unpacked in {data}, but {problem}")
        return 2
    if meta.get("unanalyzed"):
        _log(f"warning: {tag} has tracks with no rekordbox analysis: {meta['unanalyzed']}")
    if a.print_env:
        # Quoted for eval. The MDT_* lines are the production remote-library mode
        # that serves the pack's bundled ANLZ files from <data>/pioneer-share.
        for k, v in _serve_env(tag, data, meta).items():
            print(f"{k}={shlex.quote(v)}")
    else:
        env = " ".join(f"{k}={shlex.quote(v)}" for k, v in _serve_env(tag, data, meta).items() if k.startswith("MDT_"))
        _log(
            f"{tag}: {len(meta['stable_ids'])} tracks ready; start with {env} python -m apps.engine_core serve --data-dir {shlex.quote(str(data))}"
        )
        print(data)
    return 0


def _serve_env(tag: str, data: Path, meta: dict) -> dict[str, str]:
    return {
        "ODJ_FIXTURE_TAG": tag,
        "ODJ_FIXTURE_DATA_DIR": str(data),
        "ODJ_FIXTURE_IDS": ",".join(meta["stable_ids"]),
        "MDT_DATA_DIR": str(data),
        "MDT_LIBRARY_MODE": "remote",
        "MDT_CRATE_ROOT": str(data.resolve()),
        "MDT_PATH_MAP": str(data.resolve() / PATH_MAP),
    }


def _map_data_dir(data: Path) -> None:
    """Map the unpacked library onto itself in (private path), keeping its other entries.

    The printed env is remote library mode, where the app's resolver refuses
    every Mac ``/Users/...`` path the path map does not rewrite (a leftover home
    tree must not win). The default cache on a Mac is under /Users, so without
    this entry no unpacked track would play there. The entry is the identity, so
    it changes no path anywhere else, and every entry already in the file stays:
    when --data-dir names a library with its own map, that map still applies.
    """
    target, merged, changed, problem = _merged_map(data)
    if problem:
        raise RuntimeError(problem)
    if changed:
        _write_atomic(target, json.dumps(merged, indent=1))


def _merged_map(data: Path) -> tuple[Path, dict, bool, str | None]:
    """((private path), it with the identity entry, whether that differs, why the app would refuse it).

    The app loads the map before it resolves any track and refuses a malformed
    entry, so the merged map is loaded exactly as the app loads it. ensure asks
    this before it moves a library aside, so a map that would fail never costs
    the library it was meant to serve.
    """
    root = str(data.resolve())
    target = data / PATH_MAP
    raw: object = {"entries": []}
    if os.path.lexists(target):
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return target, {}, False, f"{target} is not a path map this script can extend ({e}); fix or remove it"
    if not isinstance(raw, dict) or not isinstance(raw.get("entries", []), list):
        return target, {}, False, f"{target} is not a path map this script can extend; fix or remove it"
    # Any entry for data itself or a path inside it would win over the identity
    # (production takes the longest prefix, then file order), so it is dropped.
    inside = (root + "/", root + "\\")
    keep = [
        e
        for e in raw.get("entries", [])
        if not (
            isinstance(e, dict)
            and isinstance(e.get("from"), str)
            and (e["from"].rstrip("/\\") == root or e["from"].startswith(inside))
        )
    ]
    merged = {**raw, "entries": [{"from": root, "to": root}, *keep]}
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from apps.shared.platform_paths import load_path_map

    saved = os.environ.get("MDT_PATH_MAP")
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / PATH_MAP
        probe.write_text(json.dumps(merged), encoding="utf-8")
        os.environ["MDT_PATH_MAP"] = str(probe)
        try:
            load_path_map()
        except ValueError as e:
            return target, merged, False, f"{target} holds an entry the app refuses ({e}); fix or remove it"
        finally:
            if saved is None:
                os.environ.pop("MDT_PATH_MAP")
            else:
                os.environ["MDT_PATH_MAP"] = saved
    return target, merged, merged != raw, None


def cmd_list(_: argparse.Namespace) -> int:
    for r in _releases():
        z = next((x for x in r["assets"] if x["name"] == ZIP_ASSET), None)
        print(f"{r['tag_name']}  {r['created_at']}  {(z['size'] / 1048576 if z else 0):.0f} MB")
    return 0


def _arg_text(raw: str) -> str:
    """Command-line text as the UTF-8 the shell passed, whatever the locale.

    Under a non-UTF-8 POSIX locale Python decodes argv bytes with surrogateescape,
    so a note would be stored as lone surrogates rather than its characters.
    """
    return raw.encode(sys.getfilesystemencoding(), "surrogateescape").decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--data-dir")
    src.add_argument("--zip")
    p.add_argument("--ids", default="")
    p.add_argument("--pick", type=int, default=6)
    p.add_argument("--max-audio-mb", type=float, default=400)
    p.add_argument("--with-stems", action="store_true")
    p.add_argument("--tag")
    p.add_argument("--note", default="", type=_arg_text)
    p.add_argument("--fixture-py")
    d = sub.add_parser("adopt")
    d.add_argument("--zip", required=True)
    d.add_argument("--tag")
    d.add_argument("--note", default="", type=_arg_text)
    d.add_argument("--fixture-py")
    d.add_argument(
        "--allow-unanalyzed",
        action="store_true",
        help="adopt a zip whose tracks lack ANLZ files (tests, one-off drops); pack.json lists them",
    )
    e = sub.add_parser("ensure")
    e.add_argument("--tag")
    e.add_argument("--data-dir")
    e.add_argument("--print-env", action="store_true")
    e.add_argument("--force", action="store_true", help="overwrite a --data-dir that holds a library no pack wrote")
    sub.add_parser("list")
    a = ap.parse_args()
    return {"publish": cmd_publish, "adopt": cmd_adopt, "ensure": cmd_ensure, "list": cmd_list}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
