from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

try:
    from scripts.fixtures.pack_bundle import _library, _unreachable_analysis
    from scripts.fixtures.pack_cache import _Lock, _read_meta, _resolve, _write_atomic
    from scripts.fixtures.pack_common import (
        CACHE,
        DISCARD_PREFIX,
        IMPORTER_ASSET,
        MASTER_RECORD,
        META_ASSET,
        MOVED,
        OWNED,
        SHARE_PREFIX,
        STARTING_PREFIX,
        ZIP_ASSET,
        ZIP_STAMP,
        LOCATION_RECORD,
        _log,
        _sha256,
    )
    from scripts.fixtures.pack_serve import _merged_map, _serve, _sweep_leftovers
    from scripts.fixtures.pack_verify import (
        _describes_zip,
        _done_id,
        _done_record,
        _drop_mismatched,
        _hydration_gaps,
        _location_rows,
        _master_record,
        _pack_id,
        _put_back,
        _remove_tree,
        _stamp,
        _zip_still_verified,
    )
except ImportError:
    from pack_bundle import _library, _unreachable_analysis
    from pack_cache import _Lock, _read_meta, _resolve, _write_atomic
    from pack_common import (
        CACHE,
        DISCARD_PREFIX,
        IMPORTER_ASSET,
        MASTER_RECORD,
        META_ASSET,
        MOVED,
        OWNED,
        SHARE_PREFIX,
        STARTING_PREFIX,
        ZIP_ASSET,
        ZIP_STAMP,
        LOCATION_RECORD,
        _log,
        _sha256,
    )
    from pack_serve import _merged_map, _serve, _sweep_leftovers
    from pack_verify import (
        _describes_zip,
        _done_id,
        _done_record,
        _drop_mismatched,
        _hydration_gaps,
        _location_rows,
        _master_record,
        _pack_id,
        _put_back,
        _remove_tree,
        _stamp,
        _zip_still_verified,
    )


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
