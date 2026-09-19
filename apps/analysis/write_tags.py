"""Write analyser-derived tags back into audio files (META-01 write-back).

Containers (via mutagen):

* MP3 (ID3v2.4): TBPM, TKEY, TXXX:OPENDJ_ENERGY/_SOURCE/_BACKEND_VERSION.
* MP4 / M4A:     tmpo, ----:com.apple.iTunes:initialkey, and the OPENDJ_*
                 iTunes free-form atoms.
* FLAC / OGG:    BPM, INITIALKEY, OPENDJ_* Vorbis comments.

Safety rails (mirror :mod:`apps.reconcile.apply` / ``remove_track``):

1. Typed confirm (``--confirm "WRITE TAGS TO N FILES"``) for ``--bulk``.
2. ``--live`` requires ``--i-understand-the-risks``.
3. ``pgrep -if rekordbox|djay`` warn rail (warn-only; writes proceed).
4. Timestamped JSON backup of each file's tag block pre-write.
5. Post-write verify: re-open and assert round-trip.
6. Reversal script at ``data/analysis/reversal/<sid>-<ts>.py``.
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
from typing import Any

from rich.console import Console
from rich.table import Table

from apps.shared.paths import DATA_DIR

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
    if s in (".ogg", ".oga"):
        return "ogg"
    raise ValueError(f"Unsupported container for tag write-back: {path.suffix!r}")


def _read_current_tags(path: Path) -> dict[str, str]:
    from apps.shared._mutagen import require as _require_mutagen

    _require_mutagen()
    kind = _container_kind(path)
    if kind == "mp3":
        return _read_mp3(path)
    if kind == "mp4":
        return _read_mp4(path)
    if kind == "flac":
        return _read_flac(path)
    if kind == "ogg":
        return _read_ogg(path)
    raise AssertionError(kind)  # pragma: no cover


def _write_tags(path: Path, new: dict[str, str]) -> None:
    from apps.shared._mutagen import require as _require_mutagen

    _require_mutagen()
    kind = _container_kind(path)
    if kind == "mp3":
        _write_mp3(path, new)
    elif kind == "mp4":
        _write_mp4(path, new)
    elif kind == "flac":
        _write_flac(path, new)
    elif kind == "ogg":
        _write_ogg(path, new)
    else:  # pragma: no cover
        raise AssertionError(kind)


# --- MP3 -----------------------------------------------------------------

def _read_mp3(path: Path) -> dict[str, str]:
    from mutagen.id3 import ID3, ID3NoHeaderError
    try:
        id3 = ID3(str(path))
    except ID3NoHeaderError:
        return {}
    out: dict[str, str] = {}
    if "TBPM" in id3:
        out["BPM"] = str(id3["TBPM"].text[0])
    if "TKEY" in id3:
        out["INITIALKEY"] = str(id3["TKEY"].text[0])
    for frame in id3.getall("TXXX"):
        if frame.desc.startswith(OPENDJ_NAMESPACE + "_"):
            out[frame.desc] = str(frame.text[0])
    return out


def _write_mp3(path: Path, new: dict[str, str]) -> None:
    from mutagen.id3 import ID3, TBPM, TKEY, TXXX, ID3NoHeaderError
    try:
        id3 = ID3(str(path))
    except ID3NoHeaderError:
        id3 = ID3()
    id3.add(TBPM(encoding=3, text=[new["BPM"]]))
    id3.add(TKEY(encoding=3, text=[new["INITIALKEY"]]))
    for suffix in ("ENERGY", "ENERGY_SOURCE", "BACKEND_VERSION"):
        desc = f"{OPENDJ_NAMESPACE}_{suffix}"
        id3.delall(f"TXXX:{desc}")
        id3.add(TXXX(encoding=3, desc=desc, text=[new[desc]]))
    id3.save(str(path), v2_version=4)


# --- MP4 -----------------------------------------------------------------

def _read_mp4(path: Path) -> dict[str, str]:
    from mutagen.mp4 import MP4
    mp4 = MP4(str(path))
    out: dict[str, str] = {}
    if "tmpo" in mp4:
        vals = mp4["tmpo"]
        if vals:
            out["BPM"] = str(int(vals[0]))
    key_atom = "----:com.apple.iTunes:initialkey"
    if key_atom in mp4:
        vals = mp4[key_atom]
        if vals:
            out["INITIALKEY"] = _mp4_ff_str(vals[0])
    for suffix in ("ENERGY", "ENERGY_SOURCE", "BACKEND_VERSION"):
        atom = f"----:com.apple.iTunes:{OPENDJ_NAMESPACE}_{suffix}"
        if atom in mp4:
            vals = mp4[atom]
            if vals:
                out[f"{OPENDJ_NAMESPACE}_{suffix}"] = _mp4_ff_str(vals[0])
    return out


def _write_mp4(path: Path, new: dict[str, str]) -> None:
    from mutagen.mp4 import MP4, MP4FreeForm
    mp4 = MP4(str(path))
    mp4["tmpo"] = [round(float(new["BPM"]))]
    mp4["----:com.apple.iTunes:initialkey"] = [
        MP4FreeForm(new["INITIALKEY"].encode("utf-8"), dataformat=1)
    ]
    for suffix in ("ENERGY", "ENERGY_SOURCE", "BACKEND_VERSION"):
        key = f"{OPENDJ_NAMESPACE}_{suffix}"
        atom = f"----:com.apple.iTunes:{key}"
        mp4[atom] = [MP4FreeForm(new[key].encode("utf-8"), dataformat=1)]
    mp4.save()


def _mp4_ff_str(val: Any) -> str:
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


# --- FLAC / OGG ---------------------------------------------------------

_VORBIS_FIELDS = (
    "BPM", "INITIALKEY",
    f"{OPENDJ_NAMESPACE}_ENERGY",
    f"{OPENDJ_NAMESPACE}_ENERGY_SOURCE",
    f"{OPENDJ_NAMESPACE}_BACKEND_VERSION",
)


def _read_flac(path: Path) -> dict[str, str]:
    from mutagen.flac import FLAC
    flac = FLAC(str(path))
    out: dict[str, str] = {}
    for k in _VORBIS_FIELDS:
        if k in flac and flac[k]:
            out[k] = str(flac[k][0])
    return out


def _write_flac(path: Path, new: dict[str, str]) -> None:
    from mutagen.flac import FLAC
    flac = FLAC(str(path))
    for k, v in new.items():
        flac[k] = [v]
    flac.save()


def _read_ogg(path: Path) -> dict[str, str]:
    from mutagen.oggvorbis import OggVorbis
    ogg = OggVorbis(str(path))
    out: dict[str, str] = {}
    for k in _VORBIS_FIELDS:
        if k in ogg and ogg[k]:
            out[k] = str(ogg[k][0])
    return out


def _write_ogg(path: Path, new: dict[str, str]) -> None:
    from mutagen.oggvorbis import OggVorbis
    ogg = OggVorbis(str(path))
    for k, v in new.items():
        ogg[k] = [v]
    ogg.save()


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
        except ValueError as exc:
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

    Mutagen writes in-place; if the process dies mid-write the file is left
    in an inconsistent partial state. We instead copy the original to a
    sibling tmp file, mutate that copy, fsync it, then atomically
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


def _write_reversal_script(delta: TagDelta, ts: str, backup: Path) -> Path:
    # P06-F02: the reversal script must be runnable in disaster-recovery
    # scenarios where the music-dj-tools checkout is absent or broken.
    # Inline the tag-write logic (mutagen only) so the file has no
    # project-internal imports.
    rp = reversal_path(delta.stable_id, ts)
    ns = OPENDJ_NAMESPACE
    script = f'''#!/usr/bin/env python3
"""Standalone reversal script generated by apps.analysis.write_tags @ {ts}.

Restores the pre-write tag block for {delta.path.as_posix()}. Depends only on the
system Python interpreter and ``mutagen`` (``pip install mutagen``). It
intentionally avoids importing anything from music-dj-tools so that it is
runnable during DR scenarios where the checkout is unavailable.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

OPENDJ_NAMESPACE = {ns!r}
BACKUP = Path({str(backup)!r})
AUDIO  = Path({str(delta.path)!r})

_VORBIS_FIELDS = (
    "BPM", "INITIALKEY",
    f"{{OPENDJ_NAMESPACE}}_ENERGY",
    f"{{OPENDJ_NAMESPACE}}_ENERGY_SOURCE",
    f"{{OPENDJ_NAMESPACE}}_BACKEND_VERSION",
)


def _container_kind(path: Path) -> str:
    suf = path.suffix.lower()
    if suf == ".mp3":
        return "mp3"
    if suf in (".m4a", ".mp4", ".aac"):
        return "mp4"
    if suf == ".flac":
        return "flac"
    if suf == ".ogg":
        return "ogg"
    raise SystemExit(f"unsupported container: {{path}}")


def _write_mp3(path: Path, new: dict) -> None:
    from mutagen.id3 import ID3, TBPM, TKEY, TXXX, ID3NoHeaderError
    try:
        id3 = ID3(str(path))
    except ID3NoHeaderError:
        id3 = ID3()
    id3.add(TBPM(encoding=3, text=[new["BPM"]]))
    id3.add(TKEY(encoding=3, text=[new["INITIALKEY"]]))
    for suffix in ("ENERGY", "ENERGY_SOURCE", "BACKEND_VERSION"):
        desc = f"{{OPENDJ_NAMESPACE}}_{{suffix}}"
        id3.delall(f"TXXX:{{desc}}")
        id3.add(TXXX(encoding=3, desc=desc, text=[new[desc]]))
    id3.save(str(path), v2_version=4)


def _write_mp4(path: Path, new: dict) -> None:
    from mutagen.mp4 import MP4, MP4FreeForm
    mp4 = MP4(str(path))
    mp4["tmpo"] = [int(round(float(new["BPM"])))]
    mp4["----:com.apple.iTunes:initialkey"] = [
        MP4FreeForm(new["INITIALKEY"].encode("utf-8"), dataformat=1)
    ]
    for suffix in ("ENERGY", "ENERGY_SOURCE", "BACKEND_VERSION"):
        key = f"{{OPENDJ_NAMESPACE}}_{{suffix}}"
        atom = f"----:com.apple.iTunes:{{key}}"
        mp4[atom] = [MP4FreeForm(new[key].encode("utf-8"), dataformat=1)]
    mp4.save()


def _write_vorbis(path: Path, new: dict, kind: str) -> None:
    if kind == "flac":
        from mutagen.flac import FLAC as _C
    else:
        from mutagen.oggvorbis import OggVorbis as _C
    c = _C(str(path))
    for k, v in new.items():
        c[k] = [v]
    c.save()


def main() -> int:
    data = json.loads(BACKUP.read_text(encoding="utf-8"))
    old = data["old"]
    template = {{
        "BPM": old.get("BPM", "0"),
        "INITIALKEY": old.get("INITIALKEY", ""),
        f"{{OPENDJ_NAMESPACE}}_ENERGY":
            old.get(f"{{OPENDJ_NAMESPACE}}_ENERGY", ""),
        f"{{OPENDJ_NAMESPACE}}_ENERGY_SOURCE":
            old.get(f"{{OPENDJ_NAMESPACE}}_ENERGY_SOURCE", ""),
        f"{{OPENDJ_NAMESPACE}}_BACKEND_VERSION":
            old.get(f"{{OPENDJ_NAMESPACE}}_BACKEND_VERSION", ""),
    }}
    kind = _container_kind(AUDIO)
    if kind == "mp3":
        _write_mp3(AUDIO, template)
    elif kind == "mp4":
        _write_mp4(AUDIO, template)
    else:
        _write_vorbis(AUDIO, template, kind)
    print(f"Restored tag block on {{AUDIO}}")
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
            rev = _write_reversal_script(d, ts, backup)
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
