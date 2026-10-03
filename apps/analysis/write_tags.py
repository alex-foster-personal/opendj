"""Write analyser-derived tags back into audio files (META-01 write-back).

Containers (in-house writers, Apache-2.0; see
``docs/decisions/ADR-NEW-permissive-audio-tag-io.md``):

* MP3 (ID3v2):   TBPM, TKEY, TXXX:OPENDJ_ENERGY/_SOURCE/_BACKEND_VERSION
                 via :mod:`apps.shared.id3v2`.
* FLAC:          BPM, INITIALKEY, OPENDJ_* Vorbis comments via
                 :mod:`apps.shared.flac_meta`.
* MP4 / M4A:     tmpo, ----:com.apple.iTunes:initialkey / OPENDJ_* free-form
                 atoms via :mod:`apps.shared.mp4_meta`.
* Ogg Vorbis / Opus: BPM, INITIALKEY, OPENDJ_* Vorbis comments via
                 :mod:`apps.shared.ogg_comment`.

Safety rails (mirror :mod:`apps.reconcile.apply` / ``remove_track``):

1. Typed confirm (``--confirm "WRITE TAGS TO N FILES"``) for ``--bulk``.
2. ``--live`` requires ``--i-understand-the-risks``.
3. ``pgrep -if rekordbox|djay`` warn rail (warn-only; writes proceed).
4. Timestamped JSON backup of each file's tag block pre-write.
5. Post-write verify: re-open and assert round-trip.
6. Reversal script at ``data/analysis/reversal/<sid>-<ts>.py`` (stdlib only:
   copies the byte-exact pre-write snapshot back over the file).
7. Cautious cap: ``MAX_LIVE_TRACKS = 3``.

Dry-run default: writes nothing, prints a diff table.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from apps.shared import flac_meta, id3v2, mp4_meta, ogg_comment
from apps.shared.file_rewrite import copy_extended_metadata
from apps.shared.paths import DATA_DIR
from apps.shared.vorbis_comment import VorbisCommentError

from .backends import DEFAULT_BACKEND
from .record import AnalysisRecord
from .store import fetch_records_by_ids

log = logging.getLogger("apps.analysis.write_tags")
console = Console()

CONFIRMATION_TEMPLATE = "WRITE TAGS TO {n} FILES"
MAX_LIVE_TRACKS = 3

BACKUP_ROOT: Path = DATA_DIR / "analysis" / "tag-backups"
REVERSAL_ROOT: Path = DATA_DIR / "analysis" / "reversal"
# [P06-F01 / META-02] Byte-level pre-write file snapshots so a mid-write
# failure can restore the original audio file (the JSON tag-backup under
# ``BACKUP_ROOT`` only captures the tag block, not the file bytes).
FILE_BACKUP_ROOT: Path = DATA_DIR / "analysis" / "tag-file-backups"

OPENDJ_NAMESPACE = "OPENDJ"
_OPENDJ_SUFFIXES = ("ENERGY", "ENERGY_SOURCE", "BACKEND_VERSION")
MP4_KEY_ATOM = "initialkey"  # the spelling Mixed In Key and the mutagen writer used


@dataclass
class TagDelta:
    path: Path
    stable_id: str
    old: dict[str, str]
    new: dict[str, str]

    @property
    def changed(self) -> bool:
        return any(self.old.get(k) != self.new[k] for k in self.new)


def _build_new_tags(record: AnalysisRecord) -> dict[str, str]:
    return {
        "BPM": f"{record.bpm:.2f}",
        "INITIALKEY": record.key_camelot,
        f"{OPENDJ_NAMESPACE}_ENERGY": str(record.energy),
        f"{OPENDJ_NAMESPACE}_ENERGY_SOURCE": record.energy_source,
        f"{OPENDJ_NAMESPACE}_BACKEND_VERSION": record.backend_version,
    }


def _container_kind(path: Path) -> str:
    s = path.suffix.lower()
    if s == ".mp3":
        return "mp3"
    if s in (".m4a", ".mp4", ".aac"):
        return "mp4"
    if s == ".flac":
        return "flac"
    if s in (".ogg", ".oga", ".opus"):
        return "ogg"
    raise ValueError(f"Unsupported container for tag write-back: {path.suffix!r}")


def _read_current_tags(path: Path) -> dict[str, str]:
    kind = _container_kind(path)
    if kind == "mp3":
        return _read_mp3(path)
    if kind == "mp4":
        return _read_mp4(path)
    if kind == "flac":
        return _read_vorbis(flac_meta.read(path))
    if kind == "ogg":
        return _read_vorbis(ogg_comment.read(path))
    raise AssertionError(kind)  # pragma: no cover


def _write_tags(path: Path, new: dict[str, str]) -> None:
    kind = _container_kind(path)
    if kind == "mp3":
        _write_mp3(path, new)
    elif kind == "mp4":
        _write_mp4(path, new)
    elif kind == "flac":
        meta = flac_meta.read(path)
        _set_vorbis(meta, new)
        flac_meta.save(path, meta)
    elif kind == "ogg":
        ogg = ogg_comment.read(path)
        _set_vorbis(ogg, new)
        ogg_comment.save(path, ogg)
    else:  # pragma: no cover
        raise AssertionError(kind)


def _opendj_keys() -> tuple[str, ...]:
    return tuple(f"{OPENDJ_NAMESPACE}_{suffix}" for suffix in _OPENDJ_SUFFIXES)


# --- MP3 -----------------------------------------------------------------

def _read_mp3(path: Path) -> dict[str, str]:
    tag = id3v2.read_tag(path)
    if tag is None:
        return {}
    out: dict[str, str] = {}
    for frame_id, name in (("TBPM", "BPM"), ("TKEY", "INITIALKEY")):
        value = tag.first_text(frame_id)
        if value is not None:
            out[name] = value
    for key in _opendj_keys():
        value = tag.txxx(key)
        if value is not None:
            out[key] = value
    return out


def _write_mp3(path: Path, new: dict[str, str]) -> None:
    tag = id3v2.load_or_new(path, new_version=4)
    tag.set_text("TBPM", new["BPM"])
    tag.set_text("TKEY", new["INITIALKEY"])
    for key in _opendj_keys():
        tag.set_txxx(key, new[key])
    id3v2.save(path, tag)


# --- MP4 -----------------------------------------------------------------

def _read_mp4(path: Path) -> dict[str, str]:
    meta = mp4_meta.read(path)
    out: dict[str, str] = {}
    tempo = meta.text("tmpo")
    if tempo is not None:
        out["BPM"] = tempo
    for key, name in (("INITIALKEY", MP4_KEY_ATOM), *((k, k) for k in _opendj_keys())):
        value = meta.freeform(name)
        if value is not None:
            out[key] = value
    return out


def _write_mp4(path: Path, new: dict[str, str]) -> None:
    meta = mp4_meta.read(path)
    meta.set_tempo(round(float(new["BPM"])))
    meta.set_freeform(MP4_KEY_ATOM, new["INITIALKEY"])
    for key in _opendj_keys():
        meta.set_freeform(key, new[key])
    mp4_meta.save(path, meta)


# --- FLAC / Ogg (Vorbis comments) ----------------------------------------

_VORBIS_FIELDS = ("BPM", "INITIALKEY", *_opendj_keys())


def _read_vorbis(meta: flac_meta.FlacMeta | ogg_comment.OggMeta) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in _VORBIS_FIELDS:
        value = meta.first(key)
        if value is not None:
            out[key] = value
    return out


def _set_vorbis(meta: flac_meta.FlacMeta | ogg_comment.OggMeta, new: dict[str, str]) -> None:
    for key, value in new.items():
        meta.set(key, value)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def plan_deltas(
    records: list[AnalysisRecord],
    *,
    file_map: dict[str, Path],
) -> list[TagDelta]:
    out: list[TagDelta] = []
    for rec in records:
        path = file_map.get(rec.stable_id)
        if path is None or not path.exists():
            log.warning("no file mapping (or missing file) for %s", rec.stable_id)
            continue
        try:
            old = _read_current_tags(path)
        except (ValueError, mp4_meta.Mp4Error, VorbisCommentError) as exc:
            log.warning("%s -- %s", path.name, exc)
            continue
        out.append(TagDelta(
            path=path,
            stable_id=rec.stable_id,
            old=old,
            new=_build_new_tags(rec),
        ))
    return out


# ---------------------------------------------------------------------------
# Safety rails
# ---------------------------------------------------------------------------


def pgrep_warn_rail() -> list[str]:
    """Return names of conflicting DJ apps (``rekordbox``/``djay``) currently running.

    This is a WARN-ONLY rail for tag writes: callers log offenders but
    proceed with the write. That is deliberate. Tag writes mutate audio
    files on disk, not vendor SQLite databases, so there is no live DB
    corruption risk if rekordbox/djay are open. Contrast with the DB
    writers in ``apps.reconcile.apply`` / ``remove_track``, which abort
    hard when the same pgrep returns a hit. Renamed from the old
    ``pgrep_abort_rail`` to avoid misleading future maintainers into
    assuming this has abort semantics.
    """
    offenders: list[str] = []
    for name in ("rekordbox", "djay"):
        try:
            r = subprocess.run(
                ["pgrep", "-if", name],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if r.returncode == 0 and r.stdout.strip():
                offenders.append(name)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    return offenders


def backup_path(stable_id: str, ts: str) -> Path:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    return BACKUP_ROOT / f"{stable_id}-{ts}.json"


def reversal_path(stable_id: str, ts: str) -> Path:
    REVERSAL_ROOT.mkdir(parents=True, exist_ok=True)
    return REVERSAL_ROOT / f"{stable_id}-{ts}.py"


def file_backup_path(stable_id: str, ts: str, suffix: str) -> Path:
    FILE_BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    return FILE_BACKUP_ROOT / f"{stable_id}-{ts}{suffix}"


def _snapshot_file(delta: TagDelta, ts: str) -> Path:
    """Byte-exact copy of the original audio file before any mutation.

    Returns the snapshot path so the write loop can restore it on failure.
    Uses ``shutil.copy2`` to preserve mtime/mode -- a successful write will
    not delete this; keeping it makes a manual rollback trivial.
    """
    snap = file_backup_path(delta.stable_id, ts, delta.path.suffix)
    shutil.copy2(delta.path, snap)
    return snap


def _atomic_write_tags(path: Path, new: dict[str, str]) -> None:
    """Write tags via tmp copy + fsync + os.replace for crash atomicity.

    The writers rewrite the file they are given; working on a sibling tmp
    copy means a crash mid-write can never touch the original. We copy the
    original to a sibling tmp file, mutate that copy, fsync it, then atomically
    ``os.replace`` over the original. On failure the tmp is removed and
    the original is untouched. (META-02 / P06-F01.)
    """
    parent = path.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.opendj-", suffix=path.suffix, dir=str(parent)
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(path, tmp)
        _write_tags(tmp, new)
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        copy_extended_metadata(path, tmp)  # copy2 drops macOS ACLs and xattrs
        os.replace(tmp, path)
        # Best-effort directory fsync so the rename is durable.
        try:
            dir_fd = os.open(str(parent), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _restore_from_snapshot(snapshot: Path, target: Path) -> None:
    """Restore ``target`` byte-for-byte from a prior snapshot.

    Best-effort: if the snapshot itself is missing we leave the target
    alone and let the caller surface the original failure.
    """
    if not snapshot.exists():
        return
    parent = target.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.restore-", suffix=target.suffix, dir=str(parent)
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(snapshot, tmp)
        try:
            copy_extended_metadata(target, tmp)  # the snapshot's copy2 kept no ACL
        except OSError as exc:
            # A rollback that refuses to restore the audio over lost Finder
            # tags would leave the user with the half-written file instead.
            log.warning("restore of %s keeps its bytes but not its xattrs/ACL: %s", target, exc)
        os.replace(tmp, target)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _write_backup(delta: TagDelta, ts: str) -> Path:
    bp = backup_path(delta.stable_id, ts)
    bp.write_text(
        json.dumps(
            {
                "path": str(delta.path),
                "stable_id": delta.stable_id,
                "old": delta.old,
                "captured_at": ts,
            },
            indent=2, sort_keys=True,
        ),
        encoding="utf-8",
    )
    return bp


def _write_reversal_script(delta: TagDelta, ts: str, snapshot: Path) -> Path:
    # P06-F02: the reversal script must be runnable in disaster-recovery
    # scenarios where the music-dj-tools checkout is absent or broken, so it
    # is stdlib only. It restores the byte-exact pre-write snapshot taken by
    # ``_snapshot_file`` -- stronger than re-writing the old tag values, and
    # it needs no tag library at all.
    rp = reversal_path(delta.stable_id, ts)
    script = f'''#!/usr/bin/env python3
"""Standalone reversal script generated by apps.analysis.write_tags @ {ts}.

Restores {delta.path.as_posix()} byte-for-byte from the snapshot taken before
the tag write. Standard library only.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

SNAPSHOT = Path({str(snapshot)!r})
AUDIO = Path({str(delta.path)!r})


def keep_metadata(src: str, dst: str) -> None:
    """Carry the live file's ACL and xattrs onto the restored copy, best
    effort: an attribute this filesystem or user cannot set is reported on
    stderr and skipped, so it never blocks restoring the audio itself."""
    if not os.path.exists(src):
        return
    try:
        if sys.platform == "darwin":
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            if libc.copyfile(os.fsencode(src), os.fsencode(dst), None, (1 << 0) | (1 << 2)) < 0:
                raise OSError(ctypes.get_errno(), "copyfile ACL/xattr")
            return
        if not hasattr(os, "listxattr"):
            return
        names = os.listxattr(src)
    except OSError as exc:
        print(f"warning: ACL/xattrs not kept: {{exc}}", file=sys.stderr)
        return
    for name in names:
        try:
            os.setxattr(dst, name, os.getxattr(src, name))
        except OSError as exc:
            print(f"warning: xattr {{name}} not kept: {{exc}}", file=sys.stderr)


def main() -> int:
    if not SNAPSHOT.is_file():
        print(f"snapshot missing: {{SNAPSHOT}}", file=sys.stderr)
        return 1
    fd, tmp = tempfile.mkstemp(prefix=f".{{AUDIO.name}}.restore-", dir=str(AUDIO.parent))
    os.close(fd)
    try:
        shutil.copy2(SNAPSHOT, tmp)
        keep_metadata(str(AUDIO), tmp)
        os.replace(tmp, AUDIO)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(f"Restored {{AUDIO}} from {{SNAPSHOT}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''
    rp.write_text(script, encoding="utf-8")
    rp.chmod(0o755)
    return rp


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


@dataclass
class WriteSummary:
    would_write: int = 0
    written: int = 0
    skipped_unchanged: int = 0
    failed: int = 0
    backups: list[Path] = field(default_factory=list)
    reversal_scripts: list[Path] = field(default_factory=list)


def render_diff_table(deltas: list[TagDelta]) -> Table:
    table = Table(title="tag write-back diff")
    table.add_column("path")
    table.add_column("field")
    table.add_column("old")
    table.add_column("new")
    for d in deltas:
        for k in d.new:
            old = d.old.get(k, "-")
            new = d.new[k]
            if old != new:
                table.add_row(d.path.name, k, str(old), str(new))
    return table


def apply_writes(
    deltas: list[TagDelta],
    *,
    live: bool,
    bulk: bool,
    confirm_token: str | None = None,
) -> WriteSummary:
    summary = WriteSummary()
    to_write = [d for d in deltas if d.changed]
    summary.would_write = len(to_write)

    if not live:
        console.print(render_diff_table(deltas))
        console.print(
            f"[yellow]dry-run: would write {summary.would_write} file(s); "
            "pass --live --i-understand-the-risks to apply[/yellow]"
        )
        return summary

    if not bulk and len(to_write) > MAX_LIVE_TRACKS:
        raise SystemExit(
            f"cautious mode caps writes at {MAX_LIVE_TRACKS}; pass --bulk "
            f"--confirm '{CONFIRMATION_TEMPLATE.format(n=len(to_write))}' "
            "for larger batches."
        )

    if bulk:
        expected = CONFIRMATION_TEMPLATE.format(n=len(to_write))
        if confirm_token != expected:
            raise SystemExit(
                f"bulk mode requires --confirm {expected!r}; aborting."
            )

    offenders = pgrep_warn_rail()
    if offenders:
        console.print(
            f"[yellow]warning: {', '.join(offenders)} appears to be running"
            "[/yellow]"
        )

    ts = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")

    for d in to_write:
        snapshot: Path | None = None
        try:
            backup = _write_backup(d, ts)
            summary.backups.append(backup)
            # [P06-F01 / META-02] Take a byte-exact file snapshot BEFORE
            # touching the audio file so we can restore it if anything
            # downstream raises. ``_atomic_write_tags`` publishes the new
            # bytes via a tmp-file + fsync + os.replace so a crash mid-
            # write leaves the original untouched; the post-write verify
            # below still runs against the real path.
            snapshot = _snapshot_file(d, ts)
            _atomic_write_tags(d.path, d.new)
            after = _read_current_tags(d.path)
            for k, v in d.new.items():
                got = after.get(k)
                if got is None:
                    raise RuntimeError(f"post-write verify failed: {k} missing in {d.path}")
                if k == "BPM":
                    if abs(float(got) - float(v)) > 0.5:
                        raise RuntimeError(f"BPM roundtrip drift: {got} != {v}")
                elif str(got) != str(v):
                    raise RuntimeError(f"tag roundtrip: {k}={got!r} != {v!r}")
            rev = _write_reversal_script(d, ts, snapshot)
            summary.reversal_scripts.append(rev)
            summary.written += 1
        except Exception as exc:
            log.error("write failed for %s: %s", d.path, exc)
            # Best-effort: restore the original bytes so the file is never
            # left in a partially-written state even if verify or the
            # reversal-script write raises after the atomic replace landed.
            if snapshot is not None:
                try:
                    _restore_from_snapshot(snapshot, d.path)
                except Exception as restore_exc:  # noqa: BLE001
                    log.error(
                        "restore failed for %s after write error: %s",
                        d.path, restore_exc,
                    )
            summary.failed += 1

    summary.skipped_unchanged = len(deltas) - summary.would_write
    console.print(
        f"[green]ok[/green] written={summary.written} "
        f"unchanged={summary.skipped_unchanged} failed={summary.failed}"
    )
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m apps.analysis.write_tags",
        description="Write analyser-derived tags back to audio files.",
    )
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--live", dest="dry_run", action="store_false")
    parser.add_argument("--i-understand-the-risks", action="store_true")
    parser.add_argument("--bulk", action="store_true")
    parser.add_argument("--confirm", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--stable-ids", nargs="+", default=None)
    parser.add_argument(
        "--files", nargs="+", required=True,
        help="Audio file paths (must match the --files passed to run.py).",
    )
    parser.add_argument("--stable-id-strategy", default="file-path",
                        choices=["file-path", "sha256"])
    parser.add_argument("--backend", default=DEFAULT_BACKEND)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from .run import stable_id_from_audio_bytes, stable_id_from_path
    file_map: dict[str, Path] = {}
    for p in map(Path, args.files):
        sid = (
            stable_id_from_audio_bytes(p)
            if args.stable_id_strategy == "sha256"
            else stable_id_from_path(p)
        )
        file_map[sid] = p

    live = not args.dry_run
    if live and not args.i_understand_the_risks:
        raise SystemExit("--live requires --i-understand-the-risks; aborting.")

    target_ids = args.stable_ids or list(file_map.keys())
    records = fetch_records_by_ids(target_ids, backend=args.backend)
    if args.limit:
        records = records[: args.limit]

    if not records:
        console.print(
            "[yellow]no analysis rows found; run `python -m apps.analysis.run` first"
            "[/yellow]"
        )
        return 0

    deltas = plan_deltas(records, file_map=file_map)
    if args.limit:
        deltas = deltas[: args.limit]

    summary = apply_writes(
        deltas,
        live=live,
        bulk=args.bulk,
        confirm_token=args.confirm,
    )
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
