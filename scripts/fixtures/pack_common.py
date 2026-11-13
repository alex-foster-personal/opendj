from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
