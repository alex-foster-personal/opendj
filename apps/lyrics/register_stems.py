"""Register lyric-pipeline stems as canonical in-app bundles.

THE PROBLEM THIS CLOSES (the maintainer, Tue 1 Sep 2026): ``modal_roformer_spike.py
separate`` writes flat ``<key>-vocals/<key>-instrumental`` files into the
lyrics-eval corpora with no ``manifest.json``, so the app's strict loader
(``apps/stems/artifacts.py``) cannot see them - stems on disk,
lost to the product. Registration here = write a schema-v3 ``roformer2``
bundle under ``data/state/stems-roformer-spike/<stable_id>/`` (the canonical
RoFormer root ``stem_roots()`` already searches) and then RE-LOAD it with the
strict loader as a self-check. Nothing is registered that the app cannot
actually read back.

``register_pair()`` is the single writer; the corpus sweep CLI
(``python -m apps.lyrics register-stems``) and any future batch runner MUST
route through it so generated stems can never be lost again.

Identity is resolved per corpus, honestly (unjoinable files are REPORTED,
never guessed): ``crate`` joins ``tracks.json`` (track_id -> stable_id);
``own-crate``/``oltf`` file keys are rekordbox vendor ids joined via
state.db ``track_vendor_ids``. ``novox`` is MTG-Jamendo bench data with no
library identity and is deliberately not registrable.

Audio metadata (the v3 ``audio`` block) comes from ffprobe - fail-fast if it
is missing. MP3 encoder padding can skew part lengths by a few frames, so
parts must share sample_rate/channels exactly and frame counts within
``FRAME_MISMATCH_TOL_S``; the declared count is the smaller one (the deck
mixes the overlap, never invented samples).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from apps.cloud import asset_store, hydration_core, policy
from apps.cloud.config import CloudConfig
from apps.cloud.eviction import HydrationError
from apps.lyrics.artifacts import asset_clients_for_mode
from apps.shared.paths import STATE_DB, STATE_DIR
from apps.shared.state import db as state_db_mod
from apps.shared.state import sync_stamp
from apps.stems.artifacts import ROFORMER_STEMS_DIR, load_stem_bundle

if TYPE_CHECKING:
    from apps.cloud.asset_store import AssetS3Client

LYRICS_EVAL_DIR = STATE_DIR / "lyrics-eval"
FRAME_MISMATCH_TOL_S = 0.1
REGISTRABLE_CORPORA = ("crate", "own-crate", "oltf", "batch1", "batch2")
TRACKS_JSON_CORPORA = ("crate", "batch1", "batch2")
STEM_ASSET_KIND = "stem_bundle"

#-----------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioProbe:
    sample_rate: int
    channels: int
    frame_count: int


@dataclass
class SweepReport:
    corpus: str
    registered: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    unjoinable: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"{self.corpus}: registered {len(self.registered)}, "
            f"already {len(self.already)}, unjoinable {len(self.unjoinable)}, "
            f"failed {len(self.failed)}"
        )


def _probe_audio(path: Path) -> AudioProbe:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels,duration_ts,time_base",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    stream = json.loads(out)["streams"][0]
    sample_rate = int(stream["sample_rate"])
    channels = int(stream["channels"])
    num, den = (int(x) for x in stream["time_base"].split("/"))
    frame_count = round(int(stream["duration_ts"]) * num * sample_rate / den)
    if sample_rate <= 0 or channels <= 0 or frame_count <= 0:
        raise ValueError(f"ffprobe returned non-positive audio metadata for {path}")
    return AudioProbe(sample_rate=sample_rate, channels=channels, frame_count=frame_count)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _push_bundle_parts(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    bundle_dir: Path,
    manifest: dict,
    s3: AssetS3Client,
    cfg: CloudConfig,
) -> None:
    machine_id = sync_stamp.ensure_local_machine(conn)
    resolution = hydration_core.resolve_policy(
        conn, stable_id, machine_id, asset_kind=STEM_ASSET_KIND
    )
    if resolution.mode == "excluded":
        return
    if resolution.mode == "stream":
        raise HydrationError(
            f"policy mode 'stream' is not supported for {STEM_ASSET_KIND}; "
            "use pinned or cached"
        )
    if resolution.mode not in ("pinned", "cached"):
        raise AssertionError(f"unhandled policy mode {resolution.mode!r}")

    paths_to_push = [
        bundle_dir / manifest["files"]["vocals"],
        bundle_dir / manifest["files"]["instrumental"],
        bundle_dir / "manifest.json",
    ]
    for path in paths_to_push:
        pushed = asset_store.push_asset(cfg, s3, path)
        if not asset_store.object_exists(cfg, s3, pushed.content_hash):
            raise HydrationError(
                f"{STEM_ASSET_KIND} {stable_id!r} pushed {path.name} but HEAD "
                "does not find it"
            )


#-----------------------------------------------------------------------------


@dataclass(frozen=True)
class RegisterPairStorage:
    root: Path = ROFORMER_STEMS_DIR
    s3: AssetS3Client | None = None
    cfg: CloudConfig | None = None
    conn: sqlite3.Connection | None = None


def _storage_for_register_pair(
    storage: RegisterPairStorage | None,
    root: Path | None,
) -> RegisterPairStorage:
    """Accept ``storage=`` or legacy ``root=``, never both."""
    if storage is not None and root is not None:
        raise TypeError("register_pair accepts storage= or root=, not both")
    if storage is not None:
        return storage
    return RegisterPairStorage(root=root or ROFORMER_STEMS_DIR)


def register_pair(
    *,
    stable_id: str,
    vocals: Path,
    instrumental: Path,
    model_name: str,
    model_version: str,
    source_path: str,
    storage: RegisterPairStorage | None = None,
    root: Path | None = None,
    s3: AssetS3Client | None = None,
    cfg: CloudConfig | None = None,
    conn: sqlite3.Connection | None = None,
) -> Path:
    """Write ONE canonical roformer2 bundle and verify it with the strict
    loader. Returns the bundle dir. Raises on any inconsistency - a bundle
    the app cannot read back must never be left behind (the tmp dir is
    removed on failure)."""
    if any(value is not None for value in (root, s3, cfg, conn)):
        if storage is not None:
            raise TypeError(
                "register_pair accepts storage= or legacy storage keywords, not both"
            )
        store = RegisterPairStorage(
            root=root or ROFORMER_STEMS_DIR, s3=s3, cfg=cfg, conn=conn
        )
    else:
        store = _storage_for_register_pair(storage, root)
    root = store.root
    s3 = store.s3
    cfg = store.cfg
    conn = store.conn
    if vocals.suffix.lower() != instrumental.suffix.lower():
        raise ValueError(
            f"{stable_id}: parts mix codecs ({vocals.suffix} vs {instrumental.suffix})"
        )
    voc_probe, ins_probe = _probe_audio(vocals), _probe_audio(instrumental)
    if (voc_probe.sample_rate, voc_probe.channels) != (ins_probe.sample_rate, ins_probe.channels):
        raise ValueError(f"{stable_id}: parts disagree on sample_rate/channels")
    frame_gap = abs(voc_probe.frame_count - ins_probe.frame_count)
    if frame_gap > FRAME_MISMATCH_TOL_S * voc_probe.sample_rate:
        raise ValueError(
            f"{stable_id}: frame counts differ by {frame_gap} frames "
            f"(> {FRAME_MISMATCH_TOL_S}s) - refusing to declare alignment"
        )

    src = Path(source_path)
    manifest: dict = {
        "schema_version": 3,
        "stable_id": stable_id,
        "layout": "roformer2",
        "model": {"name": model_name, "version": model_version},
        "source": {
            "path": source_path,
            "sha256": _sha256(src) if src.is_file() else "0" * 64,
        },
        "files": {
            "vocals": f"vocals{vocals.suffix.lower()}",
            "instrumental": f"instrumental{instrumental.suffix.lower()}",
        },
        "audio": {
            "sample_rate": voc_probe.sample_rate,
            "channels": voc_probe.channels,
            "frame_count": min(voc_probe.frame_count, ins_probe.frame_count),
        },
    }

    root.mkdir(parents=True, exist_ok=True)
    out_dir = root / stable_id
    tmp_dir = root / f"{stable_id}.tmp-{os.getpid()}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir()
    try:
        shutil.copy2(vocals, tmp_dir / manifest["files"]["vocals"])
        shutil.copy2(instrumental, tmp_dir / manifest["files"]["instrumental"])
        manifest["files_sha256"] = {
            part: _sha256(tmp_dir / rel) for part, rel in manifest["files"].items()
        }
        (tmp_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        tmp_dir.rename(out_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    try:
        load_stem_bundle(stable_id, stems_dir=root)
    except BaseException:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise

    if policy.CFG.mode == "cloud":
        client: AssetS3Client
        config: CloudConfig
        if s3 is not None and cfg is not None:
            client, config = s3, cfg
        else:
            got_s3, got_cfg = asset_clients_for_mode(writing=True)
            if got_s3 is None or got_cfg is None:
                raise HydrationError(
                    "cloudsync mode is 'cloud' but no S3 client was supplied for stem push"
                )
            client, config = got_s3, got_cfg
        if client is None or config is None:
            raise HydrationError(
                "cloudsync mode is 'cloud' but no S3 client was supplied for stem push"
            )
        own_conn = conn is None
        db_conn = conn if conn is not None else state_db_mod.open_rw(STATE_DB)
        try:
            _push_bundle_parts(
                db_conn,
                stable_id=stable_id,
                bundle_dir=out_dir,
                manifest=manifest,
                s3=client,
                cfg=config,
            )
        finally:
            if own_conn:
                db_conn.close()
    return out_dir


#-----------------------------------------------------------------------------
# corpus sweep


def _corpus_pairs(stems_dir: Path) -> dict[str, tuple[Path, Path]]:
    """key -> (vocals, instrumental), only complete pairs."""
    pairs: dict[str, tuple[Path, Path]] = {}
    for voc in sorted(stems_dir.glob("*-vocals.*")):
        key = voc.name[: -len("-vocals" + voc.suffix)]
        hits = list(stems_dir.glob(f"{key}-instrumental.*"))
        if len(hits) == 1:
            pairs[key] = (voc, hits[0])
    return pairs


def _tracks_json_stable_ids(corpus: str) -> dict[str, tuple[str, str]]:
    """corpus track_id -> (stable_id, real source path) from tracks.json."""
    rows = json.loads(
        (LYRICS_EVAL_DIR / corpus / "tracks.json").read_text(encoding="utf-8")
    )
    return {
        r["track_id"]: (r["stable_id"], r.get("path") or r.get("source_path") or "unknown")
        for r in rows
        if r.get("stable_id")
    }


def _vendor_stable_ids(vendor_ids: list[str], db_path: Path = STATE_DB) -> dict[str, str]:
    """rekordbox vendor_id -> stable_id via state.db (read-only)."""
    if not db_path.is_file():
        raise SystemExit(f"[ERROR] state.db missing: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        marks = ",".join("?" for _ in vendor_ids)
        rows = conn.execute(
            "SELECT vendor_id, stable_id FROM track_vendor_ids "
            f"WHERE vendor = 'rekordbox' AND vendor_id IN ({marks})",
            vendor_ids,
        ).fetchall()
    finally:
        conn.close()
    return {vid: sid for vid, sid in rows}


def _meta_model(stems_dir: Path, key: str) -> tuple[str, str, str]:
    """(model_name, model_version, source_path) from <key>-meta.json."""
    meta_path = stems_dir / f"{key}-meta.json"
    if not meta_path.is_file():
        return ("mel-band-roformer", "unknown", "unknown")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return (
        str(meta.get("config_tag") or "mel-band-roformer"),
        str(meta.get("checkpoint") or "unknown"),
        str(meta.get("source_path") or "unknown"),
    )


def sweep_corpus(corpus: str, *, write: bool, root: Path = ROFORMER_STEMS_DIR) -> SweepReport:
    if corpus not in REGISTRABLE_CORPORA:
        raise SystemExit(
            f"[ERROR] corpus {corpus!r} not registrable (novox is bench-only); "
            f"choose from {REGISTRABLE_CORPORA}"
        )
    stems_dir = LYRICS_EVAL_DIR / corpus / "stems"
    if not stems_dir.is_dir():
        raise SystemExit(f"[ERROR] no stems dir: {stems_dir}")
    pairs = _corpus_pairs(stems_dir)
    report = SweepReport(corpus=corpus)

    if corpus in TRACKS_JSON_CORPORA:
        corpus_map = _tracks_json_stable_ids(corpus)
        ident = {k: corpus_map.get(k) for k in pairs}
    else:
        vendor_map = _vendor_stable_ids(list(pairs))
        ident = {k: ((vendor_map[k], "unknown") if k in vendor_map else None) for k in pairs}

    for key, (voc, ins) in pairs.items():
        resolved = ident.get(key)
        if resolved is None:
            report.unjoinable[key] = (
                "no stable_id in tracks.json" if corpus in TRACKS_JSON_CORPORA
                else "rekordbox vendor id not in track_vendor_ids"
            )
            continue
        stable_id, src = resolved
        if (root / stable_id / "manifest.json").is_file():
            report.already.append(stable_id)
            continue
        model_name, model_version, meta_src = _meta_model(stems_dir, key)
        source_path = src if src != "unknown" else meta_src
        if not write:
            report.registered.append(stable_id)
            continue
        try:
            register_pair(
                stable_id=stable_id, vocals=voc, instrumental=ins,
                model_name=model_name, model_version=model_version,
                source_path=source_path, storage=RegisterPairStorage(root=root),
            )
            report.registered.append(stable_id)
        except (ValueError, subprocess.CalledProcessError, OSError, HydrationError) as exc:
            report.failed[key] = str(exc)
    return report
