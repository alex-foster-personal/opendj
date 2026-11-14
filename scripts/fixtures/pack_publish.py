from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from scripts.fixtures.pack_bundle import _meta, _private_repo_problem
    from scripts.fixtures.pack_cache import _copy_atomic, _write_atomic
    from scripts.fixtures.pack_common import (
        IMPORTER_ASSET,
        META_ASSET,
        REPO,
        SHARED,
        ZIP_ASSET,
        _check_tag,
        _gh,
        _importer,
        _log,
        _new_tag,
        _upload,
    )
except ImportError:
    from pack_bundle import _meta, _private_repo_problem
    from pack_cache import _copy_atomic, _write_atomic
    from pack_common import (
        IMPORTER_ASSET,
        META_ASSET,
        REPO,
        SHARED,
        ZIP_ASSET,
        _check_tag,
        _gh,
        _importer,
        _log,
        _new_tag,
        _upload,
    )


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
