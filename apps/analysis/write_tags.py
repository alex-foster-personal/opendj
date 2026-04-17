"""Write analyser-derived tags back into audio files (META-01 write-back).

Containers (via mutagen):

* MP3 (ID3v2.4): TBPM, TKEY, TXXX:OPENDJ_ENERGY/_SOURCE/_BACKEND_VERSION.
* MP4 / M4A:     tmpo, ----:com.apple.iTunes:initialkey, and the OPENDJ_*
                 iTunes free-form atoms.
* FLAC / OGG:    BPM, INITIALKEY, OPENDJ_* Vorbis comments.

Safety rails (mirror :mod:`apps.reconcile.apply` / ``remove_track``):

1. Typed confirm (``--confirm "WRITE TAGS TO N FILES"``) for ``--bulk``.
2. ``--live`` requires ``--i-understand-the-risks``.
3. ``pgrep -if rekordbox|djay`` warn rail.
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
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from apps.shared.paths import DATA_DIR

from .record import AnalysisRecord
from .store import fetch_records_by_ids

log = logging.getLogger("apps.analysis.write_tags")
console = Console()

CONFIRMATION_TEMPLATE = "WRITE TAGS TO {n} FILES"
MAX_LIVE_TRACKS = 3

BACKUP_ROOT: Path = DATA_DIR / "analysis" / "tag-backups"
REVERSAL_ROOT: Path = DATA_DIR / "analysis" / "reversal"

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
    mp4["tmpo"] = [int(round(float(new["BPM"])))]
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


def pgrep_abort_rail() -> list[str]:
    offenders: list[str] = []
    for name in ("rekordbox", "djay"):
        try:
            r = subprocess.run(
                ["pgrep", "-if", name], capture_output=True, text=True, timeout=3
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
    rp = reversal_path(delta.stable_id, ts)
    script = f'''#!/usr/bin/env python3
"""Reversal auto-generated by apps.analysis.write_tags @ {ts}.

Restores the pre-write tag block for {delta.path!s}.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import apps.analysis.write_tags as wt

BACKUP = Path({str(backup)!r})
AUDIO  = Path({str(delta.path)!r})


def main() -> int:
    data = json.loads(BACKUP.read_text(encoding="utf-8"))
    old = data["old"]
    template = {{
        "BPM": old.get("BPM", "0"),
        "INITIALKEY": old.get("INITIALKEY", ""),
        f"{{wt.OPENDJ_NAMESPACE}}_ENERGY":
            old.get(f"{{wt.OPENDJ_NAMESPACE}}_ENERGY", ""),
        f"{{wt.OPENDJ_NAMESPACE}}_ENERGY_SOURCE":
            old.get(f"{{wt.OPENDJ_NAMESPACE}}_ENERGY_SOURCE", ""),
        f"{{wt.OPENDJ_NAMESPACE}}_BACKEND_VERSION":
            old.get(f"{{wt.OPENDJ_NAMESPACE}}_BACKEND_VERSION", ""),
    }}
    wt._write_tags(AUDIO, template)
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

    offenders = pgrep_abort_rail()
    if offenders:
        console.print(
            f"[yellow]warning: {', '.join(offenders)} appears to be running"
            "[/yellow]"
        )

    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for d in to_write:
        try:
            backup = _write_backup(d, ts)
            summary.backups.append(backup)
            _write_tags(d.path, d.new)
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
    parser.add_argument("--backend", default="librosa+madmom")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from .run import stable_id_from_path, stable_id_from_audio_bytes
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
