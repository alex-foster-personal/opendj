"""Build the key-lane fixture bundle: decoded excerpts + rekordbox + MIK truth.

Homed in ``scripts/`` rather than ``apps/analysis_key/`` because it reads vendor
dbs and decodes audio. ``apps.analysis_key`` stays free of that.

This STAGES ``data/bench/key/<version>/`` (wavs, ``key-truth.json``, a pre-seal
manifest). It does NOT seal: run

    python -m apps.analysis_bench fixtures seal --lane key --version v1 \\
        --dir data/bench/key/v1

afterward. Sealing twice would rewrite identity metadata.

Excerpt policy (stated in the manifest): 60 s excerpts starting at 25 percent
of the track, 2 s guard at each end of the scoring window, decoded once to
44.1 kHz mono WAV, silence assert peak >= -40 dBFS. A track too short for a
60 s excerpt is dropped into a named ``too_short`` bucket, not folded into
``present``.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apps.adapters.rekordbox.paths import is_streaming_path
from apps.analysis_key import canon
from apps.equivalence.sources import read_rekordbox
from apps.mik.match import basename_key
from apps.shared.hashing import sha256_audio_payload
from apps.shared.paths import DATA_DIR

LANE = "key"
BUILDER_VERSION = "1.0.0"
DEFAULT_VERSION = "v1"
DEFAULT_SEED = 20260910
DEFAULT_SAMPLE_SIZE = 200
MIN_DJMD_CONTENT = 100

EXCERPT_S = 60.0
GUARD_S = 2.0
START_FRACTION = 0.25
SAMPLE_RATE = 44100
MIN_PEAK_DBFS = -40.0
MIN_DURATION_S = EXCERPT_S  # cannot fit a 60 s excerpt even centered

_VOL_RE = re.compile(r"(mean|max)_volume:\s*(-?[\d.]+) dB")
RB_SENTINEL = "All"
MIK_SENTINELS = frozenset({"", "0"})


@dataclass
class CopyRow:
    orig_path: str
    rel: str
    basename: str
    size: int | None


@dataclass
class PopulationRow:
    stable_id: str
    orig_path: str
    rel: str
    src_path: Path
    duration_s: float
    rb_scale_name: str
    mik_camelot: str
    mik_row_id: str
    mik_tier: str
    mik_confidence: float | None
    audio_payload_sha256: str = ""


@dataclass
class Denominators:
    counts: dict[str, Any] = field(default_factory=dict)
    unmatched: dict[str, int] = field(default_factory=dict)
    drops: dict[str, int] = field(default_factory=dict)

    def bump_unmatched(self, reason: str) -> None:
        self.unmatched[reason] = self.unmatched.get(reason, 0) + 1

    def bump_drop(self, reason: str) -> None:
        self.drops[reason] = self.drops.get(reason, 0) + 1


def _nfc(value: str | None) -> str | None:
    if not value:
        return None
    return unicodedata.normalize("NFC", value)


def _is_parseable_rb_key(scale_name: str | None) -> bool:
    if not scale_name or scale_name == RB_SENTINEL:
        return False
    try:
        canon.from_rekordbox_scale_name(scale_name)
    except ValueError:
        return False
    return True


def _is_parseable_mik_key(main_key: str | None) -> bool:
    if main_key is None or main_key.strip() in MIK_SENTINELS:
        return False
    try:
        canon.from_mik_camelot(main_key.strip())
    except ValueError:
        return False
    return True


def load_copy_manifest(path: Path) -> list[CopyRow]:
    rows: list[CopyRow] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            orig = _nfc(data.get("orig_path"))
            rel = data.get("rel")
            if not orig or not rel:
                continue
            rows.append(
                CopyRow(
                    orig_path=orig,
                    rel=str(rel),
                    basename=str(data.get("basename") or Path(rel).name),
                    size=int(data["size"]) if data.get("size") is not None else None,
                )
            )
    return rows


def _index_copy_manifest(
    rows: list[CopyRow],
) -> tuple[dict[str, CopyRow], dict[str, list[CopyRow]], dict[str, CopyRow]]:
    by_orig: dict[str, CopyRow] = {}
    by_base: dict[str, list[CopyRow]] = {}
    by_rel: dict[str, CopyRow] = {}
    for row in rows:
        by_orig[row.orig_path] = row
        by_rel[row.rel.replace("\\", "/")] = row
        key = basename_key(row.basename.replace("\\", "/"))
        if key:
            by_base.setdefault(key, []).append(row)
    return by_orig, by_base, by_rel


def _rel_from_windows_mik_file(file_path: str | None) -> str | None:
    """`C:\\mik-run-...\\audio\\<dir>\\<file>` -> `<dir>/<file>`, matching copy-manifest.rel.

    Basename alone is not unique in this corpus (measured 586 ambiguous of 1470).
    The Windows store's File column keeps the hashed directory the copy used.
    """
    if not file_path:
        return None
    normalised = str(file_path).replace("\\", "/")
    marker = "/audio/"
    idx = normalised.find(marker)
    if idx < 0:
        return None
    rel = normalised[idx + len(marker) :]
    return rel or None


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def _refuse_tiny_rekordbox(db_path: Path, n_content: int, *, allow_tiny: bool) -> None:
    if allow_tiny:
        return
    if n_content < MIN_DJMD_CONTENT:
        raise SystemExit(
            f"[key-bundle] {db_path} has {n_content} djmdContent rows "
            f"(need >= {MIN_DJMD_CONTENT}). That is the pruned test-shape "
            "fixture, not a live library. Place a decrypted master.plain.db of "
            "the live library at $MDT_DATA_DIR/master.plain.db (or this "
            "worktree's data/master.plain.db) and pass its sha256. Tests may "
            "pass --allow-tiny."
        )


def read_mik_windows(db_path: Path) -> list[dict[str, Any]]:
    """Windows MIKStore ``Song`` rows. Schema is not the macOS ZSONG store."""
    if not db_path.exists():
        raise SystemExit(f"[key-bundle] Windows MIK store not found: {db_path}")
    if db_path.stat().st_size == 0:
        raise SystemExit(f"[key-bundle] Windows MIK store is 0 bytes: {db_path}")
    conn = _open_ro(db_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "Song" not in tables:
            raise SystemExit(f"[key-bundle] {db_path} has no Song table; not a Windows MIKStore")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(Song)")}
        if "MainKey" not in cols:
            raise SystemExit(f"[key-bundle] {db_path} Song has no MainKey column")
        # Windows MIKStore.Song.Id is TEXT (NHibernate GUID). Tests may use
        # INTEGER SongId. Never int() the value.
        id_col = "Id" if "Id" in cols else ("SongId" if "SongId" in cols else "rowid")
        file_col = "File" if "File" in cols else "NULL"
        artist_col = "ArtistName" if "ArtistName" in cols else "NULL"
        title_col = "SongName" if "SongName" in cols else "NULL"
        conf_col = "MainKeyConfidence" if "MainKeyConfidence" in cols else "NULL"
        sql = (
            f"SELECT {id_col} AS row_id, {file_col} AS file_path, "
            f"{artist_col} AS artist, {title_col} AS title, "
            f"MainKey AS main_key, {conf_col} AS confidence FROM Song"
        )
        return [dict(r) for r in conn.execute(sql).fetchall()]
    except sqlite3.OperationalError as exc:
        raise SystemExit(f"[key-bundle] cannot read {db_path}: {exc}") from exc
    finally:
        conn.close()


def _win_basename_key(path: str | None) -> str | None:
    if not path:
        return None
    normalised = path.replace("\\", "/")
    return basename_key(normalised)


def _match_mik_to_copy(
    songs: list[dict[str, Any]],
    by_orig: dict[str, CopyRow],
    by_base: dict[str, list[CopyRow]],
    by_rel: dict[str, CopyRow],
    den: Denominators,
) -> dict[str, dict[str, Any]]:
    """rel -> winning MIK song. Ambiguous / collisions get no id, counted."""

    candidates_by_rel: dict[str, list[tuple[int, float, str, dict[str, Any], str]]] = {}
    # tuple is (tier_rank, -confidence, row_id, song, tier) for sort
    for song in songs:
        main_key = song.get("main_key")
        if not _is_parseable_mik_key(main_key):
            den.bump_unmatched("mik_no_key")
            continue
        file_path = song.get("file_path")
        nfc_file = _nfc(str(file_path).replace("\\", "/")) if file_path else None
        row_id = str(song["row_id"])
        conf = float(song["confidence"]) if song.get("confidence") is not None else 0.0
        matched: list[tuple[CopyRow, str]] = []
        if nfc_file and nfc_file in by_orig:
            matched.append((by_orig[nfc_file], "exact_path"))
        else:
            rel = _rel_from_windows_mik_file(str(file_path) if file_path else None)
            if rel and rel in by_rel:
                matched.append((by_rel[rel], "windows_rel"))
            else:
                bkey = _win_basename_key(str(file_path) if file_path else None)
                hits = by_base.get(bkey or "", [])
                if len(hits) == 1:
                    matched.append((hits[0], "basename"))
                elif len(hits) > 1:
                    den.bump_unmatched("mik_ambiguous_basename")
                    continue
        if not matched:
            den.bump_unmatched("mik_no_candidate")
            continue
        copy_row, tier = matched[0]
        tier_rank = {"exact_path": 0, "windows_rel": 1, "basename": 2}[tier]
        candidates_by_rel.setdefault(copy_row.rel, []).append(
            (tier_rank, -conf, row_id, song, tier)
        )

    winners: dict[str, dict[str, Any]] = {}
    for rel, cands in candidates_by_rel.items():
        cands.sort()
        best = cands[0]
        if len(cands) > 1:
            # several-MIK-to-one-track: keep best, count the rest
            for _extra in cands[1:]:
                den.bump_unmatched("mik_lost_collision")
        winners[rel] = {
            "song": best[3],
            "tier": best[4],
            "confidence": -best[1],
            "row_id": best[2],
        }
    return winners


def _probe_duration_s(path: Path) -> float | None:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nk=1:nw=1",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def decode_excerpt(
    src: Path, out_path: Path, *, start_s: float, excerpt_s: float
) -> dict[str, Any]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-nostdin", "-v", "info",
        "-ss", str(start_s), "-t", str(excerpt_s),
        "-i", str(src),
        "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-af", "volumedetect", "-f", "wav", "-y", str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    if proc.returncode != 0:
        return {
            "ok": False,
            "reason": f"ffmpeg exit {proc.returncode}: {proc.stderr.strip()[-180:]}",
        }
    levels = {m.group(1): float(m.group(2)) for m in _VOL_RE.finditer(proc.stderr)}
    if "max" not in levels:
        return {"ok": False, "reason": "ffmpeg reported no volume statistics"}
    if levels["max"] < MIN_PEAK_DBFS:
        return {"ok": False, "reason": f"silent excerpt: peak {levels['max']:.1f} dBFS"}
    if not out_path.is_file() or out_path.stat().st_size < 1024:
        return {"ok": False, "reason": "ffmpeg wrote no usable wav"}
    return {
        "ok": True,
        "window_start_s": start_s,
        "excerpt_s": excerpt_s,
        "mean_volume_dbfs": levels.get("mean"),
        "max_volume_dbfs": levels["max"],
        "bytes": out_path.stat().st_size,
    }


def _excerpt_start(duration_s: float, excerpt_s: float) -> float:
    start = round(duration_s * START_FRACTION, 3)
    if start + excerpt_s > duration_s:
        start = round(max(0.0, (duration_s - excerpt_s) / 2.0), 3)
    return start


def _count_rekordbox(db_path: Path) -> dict[str, int]:
    conn = _open_ro(db_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "djmdContent" not in tables:
            raise SystemExit(
                f"[key-bundle] {db_path} has no djmdContent table"
            )
        total = int(conn.execute("SELECT count(*) FROM djmdContent").fetchone()[0])
        return {"djmd_content": total}
    except sqlite3.OperationalError as exc:
        raise SystemExit(f"[key-bundle] cannot read {db_path}: {exc}") from exc
    finally:
        conn.close()


def _macos_mik_side_counts(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "placed": False,
            "path": None,
            "n_zsong": "NOT MEASURED",
            "reason": "Collection10.mikdb was not placed on this host",
        }
    if not path.exists():
        return {
            "placed": False,
            "path": str(path),
            "n_zsong": "NOT MEASURED",
            "reason": f"{path} does not exist",
        }
    from apps.equivalence.sources import read_mik

    rows = read_mik(path)
    parseable = sum(1 for r in rows if _is_parseable_mik_key(r.key_camelot))
    return {
        "placed": True,
        "path": str(path),
        "n_zsong": len(rows),
        "n_parseable_key": parseable,
    }


def build(
    *,
    version: str,
    rekordbox: Path,
    mik_windows: Path,
    audio_root: Path,
    copy_manifest: Path,
    out: Path,
    seed: int = DEFAULT_SEED,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    allow_tiny: bool = False,
    mik_macos: Path | None = None,
) -> dict[str, Any]:
    if not rekordbox.exists():
        raise SystemExit(
            f"[key-bundle] rekordbox db not found: {rekordbox}. Place a decrypted "
            "master.plain.db of the live library (djmdContent in the thousands, "
            "FolderPath values that NFC-match copy-manifest orig_path) at "
            "$MDT_DATA_DIR/master.plain.db or this worktree's data/master.plain.db "
            "and pass its sha256."
        )
    if rekordbox.stat().st_size == 0:
        raise SystemExit(
            f"[key-bundle] {rekordbox} is 0 bytes; refusing a placeholder. "
            "Need a decrypted live-library master.plain.db."
        )

    den = Denominators()
    rb_counts = _count_rekordbox(rekordbox)
    den.counts["djmd_content"] = rb_counts["djmd_content"]
    _refuse_tiny_rekordbox(rekordbox, rb_counts["djmd_content"], allow_tiny=allow_tiny)

    copy_rows = load_copy_manifest(copy_manifest)
    den.counts["copy_manifest_lines"] = len(copy_rows)
    by_orig, by_base, by_rel = _index_copy_manifest(copy_rows)

    audio_files = [p for p in audio_root.rglob("*") if p.is_file()] if audio_root.exists() else []
    den.counts["audio_root_files"] = len(audio_files)
    den.counts["audio_root"] = str(audio_root)

    rb_rows = read_rekordbox(rekordbox)
    n_non_streaming = 0
    n_rb_key = 0
    n_rb_joined = 0
    n_present = 0
    present_by_rel: dict[str, Any] = {}

    for rb in rb_rows:
        if is_streaming_path(rb.path):
            continue
        n_non_streaming += 1
        if not _is_parseable_rb_key(rb.key_raw):
            continue
        n_rb_key += 1
        orig = _nfc(rb.path)
        copy = by_orig.get(orig) if orig else None
        if copy is None:
            den.bump_unmatched("rb_not_in_copy_manifest")
            continue
        n_rb_joined += 1
        src = audio_root / copy.rel
        if not src.is_file():
            den.bump_unmatched("rb_audio_missing")
            continue
        n_present += 1
        duration = float(rb.length_raw) if rb.length_raw else None
        if duration is None or duration <= 0:
            duration = _probe_duration_s(src)
        if duration is None:
            den.bump_drop("no_duration")
            continue
        present_by_rel[copy.rel] = {
            "rb": rb,
            "copy": copy,
            "src": src,
            "duration_s": float(duration),
        }

    den.counts["djmd_non_streaming"] = n_non_streaming
    den.counts["djmd_parseable_key"] = n_rb_key
    den.counts["rb_joined_copy_manifest"] = n_rb_joined
    den.counts["present"] = n_present

    if n_present == 0 and not allow_tiny:
        raise SystemExit(
            f"[key-bundle] 0 FolderPath values resolve to a real file under "
            f"{audio_root}. Refusing to stage an empty bundle. The pruned "
            "fixture at /opt/mdt-fixtures/rekordbox/master.plain.db is not a "
            "library."
        )

    mik_songs = read_mik_windows(mik_windows)
    den.counts["mik_windows_song"] = len(mik_songs)
    den.counts["mik_windows_parseable_key"] = sum(
        1 for s in mik_songs if _is_parseable_mik_key(s.get("main_key"))
    )
    mik_winners = _match_mik_to_copy(mik_songs, by_orig, by_base, by_rel, den)

    population: list[PopulationRow] = []
    for rel, found in present_by_rel.items():
        mik = mik_winners.get(rel)
        if mik is None:
            den.bump_unmatched("present_no_mik")
            continue
        song = mik["song"]
        rb = found["rb"]
        copy = found["copy"]
        duration_s = found["duration_s"]
        if duration_s < MIN_DURATION_S and not allow_tiny:
            den.bump_drop("too_short")
            continue
        population.append(
            PopulationRow(
                stable_id=str(rb.content_id),
                orig_path=copy.orig_path,
                rel=copy.rel,
                src_path=found["src"],
                duration_s=duration_s,
                rb_scale_name=str(rb.key_raw),
                mik_camelot=str(song["main_key"]).strip(),
                mik_row_id=str(mik["row_id"]),
                mik_tier=str(mik["tier"]),
                mik_confidence=mik["confidence"],
            )
        )

    den.counts["population_present_rb_mik"] = len(population)
    population.sort(key=lambda row: row.stable_id)
    rng = random.Random(seed)
    if len(population) > sample_size:
        sampled = rng.sample(population, sample_size)
        sampled.sort(key=lambda row: row.stable_id)
        sampled_flag = True
    else:
        sampled = population
        sampled_flag = False
    den.counts["sample_size_requested"] = sample_size
    den.counts["n_sampled"] = len(sampled)
    den.counts["sampled"] = sampled_flag
    den.counts["seed"] = seed

    if out.exists():
        for meta in ("SHA256SUMS", "BUNDLE_ID"):
            (out / meta).unlink(missing_ok=True)
        wav_dir = out / "wav"
        if wav_dir.exists():
            shutil.rmtree(wav_dir)
    wav_dir = out / "wav"
    wav_dir.mkdir(parents=True, exist_ok=True)

    fixtures: list[dict[str, Any]] = []
    truth: dict[str, dict[str, str]] = {}
    for row in sampled:
        excerpt_s = EXCERPT_S if row.duration_s >= EXCERPT_S else max(0.1, row.duration_s)
        if allow_tiny and row.duration_s < EXCERPT_S:
            excerpt_s = max(0.1, row.duration_s)
        start = _excerpt_start(row.duration_s, excerpt_s)
        dest = wav_dir / f"{row.stable_id}.wav"
        decoded = decode_excerpt(row.src_path, dest, start_s=start, excerpt_s=excerpt_s)
        if not decoded["ok"]:
            reason = decoded["reason"]
            bucket = "silent" if "silent" in reason else "ffmpeg_fail"
            den.bump_drop(bucket)
            if dest.exists():
                dest.unlink()
            continue
        payload_hash = sha256_audio_payload(row.src_path)
        fixtures.append(
            {
                "stable_id": row.stable_id,
                "wav": f"wav/{row.stable_id}.wav",
                "window_start_s": decoded["window_start_s"],
                "window_end_s": round(decoded["window_start_s"] + decoded["excerpt_s"], 3),
                "score_start_s": round(decoded["window_start_s"] + GUARD_S, 3),
                "score_end_s": round(
                    decoded["window_start_s"] + decoded["excerpt_s"] - GUARD_S, 3
                ),
                "excerpt_s": decoded["excerpt_s"],
                "source_duration_s": row.duration_s,
                "audio_payload_sha256": payload_hash,
                "rekordbox_scale_name": row.rb_scale_name,
                "mik_camelot": row.mik_camelot,
                "orig_path": row.orig_path,
                "rel": row.rel,
                "mik_tier": row.mik_tier,
                "peak_dbfs": decoded["max_volume_dbfs"],
            }
        )
        truth[row.stable_id] = {
            "rekordbox": row.rb_scale_name,
            "mik_camelot": row.mik_camelot,
        }

    den.counts["n_fixtures"] = len(fixtures)
    if not fixtures:
        raise SystemExit(
            "[key-bundle] staged 0 fixtures after decode; refusing an empty bundle. "
            f"drops={den.drops} unmatched={den.unmatched}"
        )

    macos = _macos_mik_side_counts(mik_macos)
    den.counts["mik_macos"] = macos

    (out / "key-truth.json").write_text(
        json.dumps({"schema": 1, "keys": truth}, indent=1),
        encoding="utf-8",
    )
    manifest = {
        "lane": LANE,
        "version": version,
        "builder_version": BUILDER_VERSION,
        "paths_relative_to": "manifest",
        "stable_id": "rekordbox djmdContent.ID as string",
        "mik_truth_store": "windows",
        "excerpt_policy": {
            "excerpt_s": EXCERPT_S,
            "guard_s": GUARD_S,
            "start_fraction": START_FRACTION,
            "sample_rate": SAMPLE_RATE,
            "min_peak_dbfs": MIN_PEAK_DBFS,
            "why": (
                "identical decoded bytes for every candidate; portable bundle; "
                "S-KEY's inference window is 15 s so 60 s is 4x that; full-track "
                "WAVs of the 12 GB source would not be a bundle anyone pulls"
            ),
        },
        "reads": {
            "truth": "key-truth.json",
            "checksums": "SHA256SUMS",
            "scorer": "apps/analysis_bench/scorers/key_lane.py (version stamped in every artifact)",
        },
        "seed": seed,
        "sample_size": sample_size,
        "denominators": {**den.counts, "unmatched": den.unmatched, "drops": den.drops},
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "built_from": str(out.resolve()),
        "fixtures": fixtures,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(
        f"[key-bundle] staged {out}: {len(fixtures)} fixtures, "
        f"population {den.counts['population_present_rb_mik']}, "
        f"sampled={sampled_flag}",
        flush=True,
    )
    print("[key-bundle] not sealed; run `python -m apps.analysis_bench fixtures seal "
          f"--lane key --version {version} --dir {out}`", flush=True)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.build_key_bundle")
    sub = parser.add_subparsers(dest="command", required=True)
    build_cmd = sub.add_parser("build", help="stage decoded excerpts + truth")
    build_cmd.add_argument("--version", default=DEFAULT_VERSION)
    build_cmd.add_argument("--rekordbox", required=True, type=Path)
    build_cmd.add_argument("--mik-windows", required=True, type=Path)
    build_cmd.add_argument("--mik-macos", type=Path, default=None)
    build_cmd.add_argument("--audio-root", required=True, type=Path)
    build_cmd.add_argument("--copy-manifest", required=True, type=Path)
    build_cmd.add_argument("--seed", type=int, default=DEFAULT_SEED)
    build_cmd.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    build_cmd.add_argument(
        "--out",
        type=Path,
        default=None,
        help="default data/bench/key/<version>/",
    )
    build_cmd.add_argument(
        "--allow-tiny",
        action="store_true",
        help="permit <100 djmdContent rows / short wavs; tests only",
    )
    args = parser.parse_args(argv)
    out = args.out or (DATA_DIR / "bench" / LANE / args.version)
    build(
        version=args.version,
        rekordbox=args.rekordbox,
        mik_windows=args.mik_windows,
        audio_root=args.audio_root,
        copy_manifest=args.copy_manifest,
        out=out,
        seed=args.seed,
        sample_size=args.sample_size,
        allow_tiny=args.allow_tiny,
        mik_macos=args.mik_macos,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
