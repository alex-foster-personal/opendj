"""Gather one batch of files into ~/Music/_ingest/<batch>/ by APFS clone copy.

Copies, never moves: sources stay where they were (delete nothing). ``cp -c``
clones on APFS, so a same-volume copy costs no extra space. A source that is an
iCloud dataless stub or 0 bytes is a hard error before anything is copied.
"""
from __future__ import annotations

import filecmp
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from apps.rekordbox_ingestion.config import CFG
from apps.rekordbox_ingestion.status import list_batch_audio
from apps.shared import fs_residency


@dataclass
class StagePlan:
    batch_dir: Path
    copy: list[tuple[Path, Path]]
    already_there: list[Path]


def read_source_list(list_file: Path) -> list[Path]:
    """First tab-separated column of each non-empty, non-# line is a path."""
    paths = [
        Path(line.split("\t", 1)[0].strip()).expanduser()
        for line in list_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not paths:
        raise ValueError(f"no paths in {list_file}")
    return paths


def sources_from_dir(source_dir: Path) -> list[Path]:
    return list_batch_audio(source_dir)


def plan_stage(sources: list[Path], batch_dir: Path) -> StagePlan:
    """Validate every source and map it to its batch path. Raises on any bad source."""
    problems: list[str] = []
    copy: list[tuple[Path, Path]] = []
    already: list[Path] = []
    seen: dict[str, Path] = {}
    for source in sources:
        if not source.is_file():
            problems.append(f"missing: {source}")
            continue
        if source.suffix.lower() not in CFG.AUDIO_SUFFIXES:
            problems.append(f"not audio: {source}")
            continue
        if source.stat().st_size == 0 or not fs_residency.is_materialised(source):
            problems.append(f"empty or iCloud dataless: {source}")
            continue
        # Keyed case-insensitively: the default macOS and Windows volumes treat
        # Track.mp3 and track.mp3 as one file, so the second copy would overwrite the first.
        name_key = source.name.casefold()
        if name_key in seen and seen[name_key] != source:
            problems.append(f"two sources share the name {source.name}: {seen[name_key]} and {source}")
            continue
        seen[name_key] = source
        destination = batch_dir / source.name
        if source.resolve() == destination.resolve():
            already.append(destination)
        elif destination.exists():
            if filecmp.cmp(source, destination, shallow=False):
                already.append(destination)
            else:
                problems.append(f"different file already at {destination}")
        else:
            copy.append((source, destination))
    if problems:
        raise ValueError("refusing to stage:\n  " + "\n  ".join(problems))
    return StagePlan(batch_dir=batch_dir, copy=copy, already_there=already)


def _copy_file(source: Path, destination: Path, *, same_volume: bool) -> None:
    """Clone on macOS when both paths share a volume, else a plain metadata-keeping copy."""
    if sys.platform == "darwin":
        # clonefile(2) only works within one volume; across volumes a real copy is the job.
        clone = ["-c"] if same_volume else []
        subprocess.run(["cp", *clone, "-p", "-n", str(source), str(destination)], check=True)
    else:
        shutil.copy2(source, destination)


def execute_stage(plan: StagePlan) -> None:
    plan.batch_dir.mkdir(parents=True, exist_ok=True)
    batch_device = plan.batch_dir.stat().st_dev
    for source, destination in plan.copy:
        _copy_file(source, destination, same_volume=source.stat().st_dev == batch_device)
        if destination.stat().st_size != source.stat().st_size:
            raise RuntimeError(f"copy size mismatch: {source} -> {destination}")
