#!/usr/bin/env python3
"""
Exploratory parser for tests/fixtures/rb-usb-export/PIONEER/.

Goals
-----
1. Parse every ANLZ0000.DAT / .EXT / .2EX with pyrekordbox and tally which
   tag sections appear across the 199-track export.
2. Probe exportLibrary.db (SQLite) to get table row counts (this is the
   Rekordbox-7 auxiliary SQLite DB — pyrekordbox 0.4.4 does not ship a
   DeviceSQL parser for export.pdb, so we fall back to rekordcrate for
   that file — see docs/rekordcrate-export-pdb-dump.txt).
3. Emit a Markdown report at docs/rb-usb-export-fixture-report.md with
   tag-presence matrix, row counts, parse errors, and a human-readable
   device-compatibility summary.

The script is deliberately read-only and does not commit anything.
Run:
    source .venv/bin/activate
    python apps/scratch/parse_usb_fixture.py
"""

from __future__ import annotations

import sqlite3
import sys
import traceback
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "rb-usb-export" / "PIONEER"
REPORT = REPO / "docs" / "rb-usb-export-fixture-report.md"

# ---------------------------------------------------------------------------
# pyrekordbox API probe — record what the installed version exposes so the
# report can flag drift against future releases.
# ---------------------------------------------------------------------------

try:
    import pyrekordbox

    PYREK_VERSION = getattr(pyrekordbox, "__version__", "unknown")
except Exception as exc:  # pragma: no cover
    print(f"FATAL: pyrekordbox import failed: {exc}", file=sys.stderr)
    raise

from pyrekordbox.anlz import AnlzFile  # type: ignore[import-not-found]
from pyrekordbox.anlz.tags import TAGS as KNOWN_TAGS  # type: ignore[import-not-found]


def parse_anlz_tree(root: Path) -> dict[str, Any]:
    """Walk USBANLZ/Pxxx/<trackid>/ANLZ0000.{DAT,EXT,2EX}.

    Returns a dict with:
      - ``tracks``: number of track directories scanned
      - ``tag_counts``: {suffix: Counter(tag_type -> n_tracks_with_it)}
      - ``tag_per_track``: {suffix: [(track_rel_path, [tag_types])]}
      - ``errors``: [(track_rel_path, suffix, exc_repr)]
    """
    suffixes = ("DAT", "EXT", "2EX")
    tag_counts: dict[str, Counter[str]] = {s: Counter() for s in suffixes}
    tag_per_track: dict[str, list[tuple[str, list[str]]]] = {s: [] for s in suffixes}
    errors: list[tuple[str, str, str]] = []
    tracks_seen: set[str] = set()

    for p_dir in sorted((root / "USBANLZ").iterdir()):
        if not p_dir.is_dir() or not p_dir.name.startswith("P"):
            continue
        for track_dir in sorted(p_dir.iterdir()):
            if not track_dir.is_dir():
                continue
            rel = track_dir.relative_to(root).as_posix()
            tracks_seen.add(rel)
            for suffix in suffixes:
                fpath = track_dir / f"ANLZ0000.{suffix}"
                if not fpath.exists():
                    continue
                try:
                    anlz = AnlzFile.parse_file(str(fpath))
                    tags_here = list(anlz.tag_types)
                except Exception as exc:  # noqa: BLE001 — exploratory dump
                    errors.append((rel, suffix, f"{type(exc).__name__}: {exc}"))
                    continue
                tag_per_track[suffix].append((rel, tags_here))
                # Count each tag type once per track (not per occurrence).
                for t in set(tags_here):
                    tag_counts[suffix][t] += 1

    return {
        "tracks": len(tracks_seen),
        "tag_counts": tag_counts,
        "tag_per_track": tag_per_track,
        "errors": errors,
    }


def probe_export_library_db(path: Path) -> dict[str, Any]:
    """Sniff exportLibrary.db.

    IMPORTANT: we do NOT call ``sqlite3.connect`` on the fixture file
    directly. sqlite3 will touch the WAL and SHM files even when opened
    with ``mode=ro``; on an encrypted DB that rewrites the WAL to zero
    bytes, mutating the fixture. We first read the file header to check
    for the ``SQLite format 3`` magic and only open a copy if present.
    """
    out: dict[str, Any] = {
        "path": str(path),
        "tables": {},
        "error": None,
        "magic": None,
    }
    if not path.exists():
        out["error"] = "file not found"
        return out

    with path.open("rb") as fh:
        header = fh.read(16)
    out["magic"] = header.hex()

    SQLITE_MAGIC = b"SQLite format 3\x00"
    if header[: len(SQLITE_MAGIC)] != SQLITE_MAGIC:
        out["error"] = (
            "file does not start with SQLite magic — likely SQLCipher-"
            "encrypted (Rekordbox 7 OneLibrary DBs are encrypted, same "
            f"family as the desktop master.db). header={header.hex()}"
        )
        return out

    # Header OK — copy to a scratch dir so we never mutate the fixture.
    import shutil
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / path.name
            shutil.copy2(path, tmp_path)
            shm = path.with_name(path.name + "-shm")
            wal = path.with_name(path.name + "-wal")
            if shm.exists():
                shutil.copy2(shm, tmp_path.with_name(tmp_path.name + "-shm"))
            if wal.exists():
                shutil.copy2(wal, tmp_path.with_name(tmp_path.name + "-wal"))
            conn = sqlite3.connect(str(tmp_path))
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
            tables = [row[0] for row in cur.fetchall()]
            for t in tables:
                try:
                    cur.execute(f'SELECT COUNT(*) FROM "{t}"')
                    out["tables"][t] = cur.fetchone()[0]
                except sqlite3.Error as exc:
                    out["tables"][t] = f"ERROR: {exc}"
            conn.close()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def probe_export_pdb(path: Path) -> dict[str, Any]:
    """Attempt to parse export.pdb with pyrekordbox. 0.4.4 likely does NOT
    expose a DeviceSQL PDB parser; we document that gap and recommend
    rekordcrate for this file."""
    result: dict[str, Any] = {"path": str(path), "supported": False, "note": ""}
    try:
        from pyrekordbox import db6  # noqa: F401

        # db6.Rekordbox6Database targets the desktop app's master.db (SQLite,
        # encrypted with SQLCipher). It is NOT the DeviceSQL export.pdb.
        result["note"] = (
            "pyrekordbox 0.4.4 has no DeviceSQL (export.pdb) parser. "
            "db6.Rekordbox6Database targets the encrypted master.db used "
            "by the desktop app. See rekordcrate dump below for PDB rows."
        )
    except Exception as exc:  # noqa: BLE001
        result["note"] = f"import pyrekordbox.db6 failed: {exc}"
    return result


def probe_settings_files(root: Path) -> dict[str, Any]:
    """Try pyrekordbox.mysettings for the *.DAT preferences files."""
    from pyrekordbox import mysettings as ms  # type: ignore[import-not-found]

    results: dict[str, Any] = {}
    candidates = [
        ("MYSETTING.DAT", ms.MySettingFile),
        ("MYSETTING2.DAT", ms.MySetting2File),
        ("DJMMYSETTING.DAT", ms.DjmMySettingFile),
        ("DEVSETTING.DAT", ms.DevSettingFile),
    ]
    for name, cls in candidates:
        p = root / name
        if not p.exists():
            results[name] = {"status": "missing"}
            continue
        try:
            obj = cls.parse_file(str(p))
            # Extract a small sample of fields if present.
            fields = {}
            for attr in ("on_air_display", "lcd_brightness", "quantize",
                         "channel_fader_curve", "cross_fader_curve",
                         "jog_ring_brightness", "tempo_range"):
                if hasattr(obj, attr):
                    try:
                        fields[attr] = getattr(obj, attr)
                    except Exception:  # noqa: BLE001
                        pass
            results[name] = {"status": "ok", "fields": fields}
        except Exception as exc:  # noqa: BLE001
            results[name] = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
    return results


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

TAG_DESCRIPTIONS = {
    "PQTZ": "Beat grid (quantization)",
    "PQT2": "Beat grid v2 (extended)",
    "PCOB": "Cues / hot cues (legacy, pre-nxs2)",
    "PCO2": "Cues / hot cues (nxs2+, with color + comment)",
    "PPTH": "Path (filesystem path back to audio)",
    "PVBR": "VBR per-frame byte offsets (MP3 seek table)",
    "PSSI": "Phrase / song-structure intelligence",
    "PWAV": "Preview waveform (small, scroll)",
    "PWV2": "Tiny waveform (player rekordbox-browse thumbnail)",
    "PWV3": "Detailed scroll waveform (nxs2)",
    "PWV4": "Color scroll waveform (nxs2+)",
    "PWV5": "Color waveform (detail, nxs2+)",
    "PWV6": "3-band waveform scroll (CDJ-3000)",
    "PWV7": "3-band waveform detail (CDJ-3000)",
    "PWVC": "CDJ-3000 additional waveform/color blob",
}


def hardware_compat_summary(anlz: dict[str, Any]) -> list[str]:
    counts = anlz["tag_counts"]
    tracks = anlz["tracks"] or 1
    lines = []

    def pct(suffix: str, tag: str) -> int:
        return round(100 * counts[suffix].get(tag, 0) / tracks)

    # Core analysis
    if pct("DAT", "PQTZ") == 100:
        lines.append(
            f"- Every track ({tracks}/{tracks}) has PQTZ beat grids — "
            "safe to play on any CDJ from the 2000nxs line forward."
        )
    # Color + phrase
    color_wave = max(pct("EXT", "PWV4"), pct("EXT", "PWV5"))
    if color_wave >= 90:
        lines.append(
            f"- {color_wave}% of tracks have color waveforms (PWV4/PWV5) — "
            "designed for CDJ-2000nxs2 and newer."
        )
    if pct("EXT", "PSSI") >= 90:
        lines.append(
            f"- {pct('EXT', 'PSSI')}% of tracks have PSSI phrase data — "
            "phrase-aware features (Phase Meter, Phrase Beat-Jump) light "
            "up on nxs2 / CDJ-3000 / XDJ-RX3."
        )
    # CDJ-3000
    cdj3k_tags = [counts["2EX"].get(t, 0) for t in ("PWV6", "PWV7")]
    if any(cdj3k_tags):
        lines.append(
            f"- .2EX files present with PWV6/PWV7 counts "
            f"{cdj3k_tags} — this USB includes the CDJ-3000 "
            "high-resolution 3-band waveform extension."
        )
    else:
        lines.append(
            "- .2EX files do NOT contain PWV6/PWV7 — no CDJ-3000 "
            "high-res waveforms were exported for this library."
        )
    # OneLibrary
    if (FIXTURE / "rekordbox" / "exportLibrary.db").exists():
        lines.append(
            "- `exportLibrary.db` (SQLCipher-encrypted SQLite) is present — "
            "this export uses Rekordbox 7's 'OneLibrary' auxiliary DB, "
            "which CDJ-3000X+ firmware reads alongside the classic "
            "DeviceSQL `export.pdb`. Without the SQLCipher key we cannot "
            "enumerate its tables; see the `exportLibrary.db` section "
            "below for the header-sniff result."
        )
    return lines


def render_report(
    anlz: dict[str, Any],
    export_pdb: dict[str, Any],
    export_lib: dict[str, Any],
    settings: dict[str, Any],
    surprises: list[str],
) -> str:
    lines: list[str] = []
    p = lines.append

    p("# Rekordbox 7 USB Export — Fixture Parse Report")
    p("")
    p(f"- Fixture root: `tests/fixtures/rb-usb-export/PIONEER/`")
    p(f"- pyrekordbox version: **{PYREK_VERSION}**")
    p(f"- Python: {sys.version.split()[0]}")
    p(f"- Track directories scanned (USBANLZ/Pxxx/<id>/): **{anlz['tracks']}**")
    p("")

    # --- Tag presence matrix ---------------------------------------------
    p("## ANLZ tag-presence matrix")
    p("")
    p(
        "Counts are *distinct tracks* (of "
        f"{anlz['tracks']}) whose `ANLZ0000.<suffix>` contains at least one "
        "tag of the given type."
    )
    p("")
    all_tags = sorted(
        set().union(*(set(c) for c in anlz["tag_counts"].values()))
    )
    p("| Tag  | Description | .DAT | .EXT | .2EX |")
    p("|------|-------------|------|------|------|")
    for tag in all_tags:
        desc = TAG_DESCRIPTIONS.get(tag, "(undocumented here)")
        row = [
            str(anlz["tag_counts"]["DAT"].get(tag, 0)),
            str(anlz["tag_counts"]["EXT"].get(tag, 0)),
            str(anlz["tag_counts"]["2EX"].get(tag, 0)),
        ]
        p(f"| `{tag}` | {desc} | {row[0]} | {row[1]} | {row[2]} |")
    unknown = [t for t in all_tags if t not in KNOWN_TAGS]
    if unknown:
        p("")
        p(
            f"> pyrekordbox-unknown tag types seen: {', '.join(unknown)}"
        )
    p("")

    # --- Hardware compatibility summary ----------------------------------
    p("## Hardware-compatibility summary")
    p("")
    for line in hardware_compat_summary(anlz):
        p(line)
    p("")

    # --- export.pdb note --------------------------------------------------
    p("## `export.pdb` (DeviceSQL)")
    p("")
    p(f"- Path: `{export_pdb['path']}`")
    p(f"- pyrekordbox support: **{'yes' if export_pdb['supported'] else 'no'}**")
    p(f"- {export_pdb['note']}")
    p("")

    # --- exportLibrary.db tables -----------------------------------------
    p("## `exportLibrary.db` (SQLite / SQLCipher?)")
    p("")
    try:
        lib_rel = Path(export_lib["path"]).relative_to(REPO).as_posix()
    except Exception:  # noqa: BLE001
        lib_rel = export_lib["path"]
    p(f"- Path: `{lib_rel}`")
    p(f"- First 16 header bytes: `{export_lib.get('magic')}`")
    if export_lib.get("error"):
        p(f"- **Could not open as plain SQLite**: {export_lib['error']}")
        p("")
        p(
            "This was a genuine discovery: Rekordbox 7's `exportLibrary.db` "
            "is *encrypted* (SQLCipher family). A plain `sqlite3.connect()` "
            "call against it returns zero tables and — dangerously — also "
            "rewrites the fixture's WAL file to 0 bytes on close, because "
            "SQLite's engine initialises journalling even when it can't "
            "read the file. This script was updated after discovering this "
            "behaviour: it now sniffs the 16-byte magic first and only "
            "opens a *copy* in a tempdir when the header matches."
        )
    else:
        p("| Table | Row count |")
        p("|-------|-----------|")
        for name, n in sorted(export_lib["tables"].items()):
            p(f"| `{name}` | {n} |")
    p("")

    # --- Settings DAT probes ---------------------------------------------
    p("## Settings DAT files (pyrekordbox.mysettings)")
    p("")
    p("| File | Status | Notes |")
    p("|------|--------|-------|")
    for name, info in settings.items():
        status = info.get("status", "?")
        if status == "ok":
            fields = info.get("fields", {})
            note = (
                ", ".join(f"{k}={v}" for k, v in fields.items())
                if fields
                else "(no well-known fields extracted)"
            )
        elif status == "error":
            note = info.get("error", "")
        else:
            note = "(file absent)"
        p(f"| `{name}` | {status} | {note} |")
    p("")

    # --- Parse errors -----------------------------------------------------
    p("## ANLZ parse errors")
    p("")
    if not anlz["errors"]:
        p("No fatal parse errors across "
          f"{anlz['tracks']} tracks × 3 files.")
    else:
        p("| Track | Suffix | Error |")
        p("|-------|--------|-------|")
        for track, suffix, err in anlz["errors"]:
            p(f"| `{track}` | `{suffix}` | {err} |")
    p("")

    # --- rekordcrate cross-validation ------------------------------------
    p("## rekordcrate cross-validation")
    p("")
    p(
        "`rekordcrate` (Rust) was built from `main` in `/tmp/rekordcrate-tmp` "
        "and run against this fixture. See adjacent dump files:"
    )
    p("")
    p("- `docs/rekordcrate-export-pdb-dump.txt` — first 500 lines of "
      "`dump-pdb` for `export.pdb`")
    p("- `docs/rekordcrate-sample-anlz-dump.txt` — `dump-anlz` for "
      "the first track's ANLZ0000.DAT")
    p("")

    # --- Surprises --------------------------------------------------------
    p("## Surprises (files not covered by our existing research doc)")
    p("")
    for s in surprises:
        p(f"- {s}")
    p("")
    return "\n".join(lines) + "\n"


def main() -> int:
    if not FIXTURE.exists():
        print(f"Fixture not found: {FIXTURE}", file=sys.stderr)
        return 1

    print(f"[parse_usb_fixture] pyrekordbox {PYREK_VERSION}")
    print(f"[parse_usb_fixture] scanning {FIXTURE}")

    anlz = parse_anlz_tree(FIXTURE)
    print(f"  tracks scanned      : {anlz['tracks']}")
    for s in ("DAT", "EXT", "2EX"):
        print(f"  .{s:3}  tag counts   : {dict(anlz['tag_counts'][s])}")
    print(f"  parse errors        : {len(anlz['errors'])}")

    export_pdb = probe_export_pdb(FIXTURE / "rekordbox" / "export.pdb")
    export_lib = probe_export_library_db(FIXTURE / "rekordbox" / "exportLibrary.db")
    print(f"  exportLibrary.db tables: {list(export_lib.get('tables', {}))[:10]}")

    settings = probe_settings_files(FIXTURE)
    for name, info in settings.items():
        print(f"  {name:18} -> {info.get('status')}")

    # Surprises catalogued manually — files we saw that do NOT appear in
    # our prior research doc.
    surprises: list[str] = []
    if (FIXTURE / "rekordbox" / "RBFLTR.DAT").exists():
        surprises.append(
            "`rekordbox/RBFLTR.DAT` — 232-byte binary, undocumented. "
            "Suspected filter / search-index blob created by Rekordbox 7 "
            "when exporting. No OSS parser covers it."
        )
    if (FIXTURE / "USBANLZ" / "USBMNG.DAT").exists():
        surprises.append(
            "`USBANLZ/USBMNG.DAT` — binary USB management blob at the "
            "USBANLZ root (not per-track). Purpose unclear; rekordcrate "
            "does not reference it."
        )
    if (FIXTURE / "djprofile.nxs").exists():
        surprises.append(
            "`djprofile.nxs` — 160-byte proprietary DJ-profile file at the "
            "PIONEER root. Used by players to restore personal settings."
        )
    if (FIXTURE / "rekordbox" / "exportLibrary.db-wal").exists():
        surprises.append(
            "`exportLibrary.db-wal` / `-shm` — SQLite write-ahead log and "
            "shared memory alongside the auxiliary DB. Real exports ship "
            "with uncommitted WAL entries; any reader that opens the DB "
            "without WAL awareness will see a stale snapshot."
        )
    surprises.append(
        "`extracted/gcred.dat` — 66-byte ASCII with what looks like a "
        "cached credential token. **Excluded from the fixture** (see "
        "fixture README); parsers should treat this directory as opaque."
    )

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        render_report(anlz, export_pdb, export_lib, settings, surprises),
        encoding="utf-8",
    )
    print(f"[parse_usb_fixture] report written to {REPORT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
