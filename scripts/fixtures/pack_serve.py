from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

try:
    from scripts.fixtures.pack_cache import _write_atomic
    from scripts.fixtures.pack_common import (
        CACHE,
        DISCARD_PREFIX,
        IMPORTER_ASSET,
        META_ASSET,
        PATH_MAP,
        REPO,
        STARTING_PREFIX,
        ZIP_ASSET,
        _log,
        _releases,
    )
    from scripts.fixtures.pack_verify import _remove_tree
except ImportError:
    from pack_cache import _write_atomic
    from pack_common import (
        CACHE,
        DISCARD_PREFIX,
        IMPORTER_ASSET,
        META_ASSET,
        PATH_MAP,
        REPO,
        STARTING_PREFIX,
        ZIP_ASSET,
        _log,
        _releases,
    )
    from pack_verify import _remove_tree


def _sweep_leftovers(data: Path, done: Path, force: bool) -> None:
    """Remove what a killed run left under this script's own prefixes, in a dir it may write.

    A discard dir exists only after an import began here, so the completion or
    importing marker is there with it; a starting dir is made for an owned dir, or
    for one --force let it take. Anywhere else a name with the prefix is not the
    script's to delete, so without a marker or --force nothing is touched.
    """
    if not (done.exists() or (data / ".fixture-pack.importing").exists() or force):
        return
    for left in data.glob(f"{DISCARD_PREFIX}*"):
        _remove_tree(left)  # a successful import's leftover, killed while deleting it
    for left in data.glob(f"{STARTING_PREFIX}*"):
        _remove_tree(left)  # an aside killed before it was renamed into place: no library in it


def _point_current(data: Path) -> str | None:
    """Record data as the current pack dir, or say why not.

    CACHE/current.txt holds the path on every platform and must be written. The
    CACHE/current link is a convenience only a link may hold: a file or directory
    named current is someone else's and stops the run, untouched. Making the link
    can fail where links need a privilege (Windows without Developer Mode); then an
    earlier link is removed rather than left naming another dir, and a warning says so.
    """
    current = CACHE / "current"
    if os.path.lexists(current) and not current.is_symlink():
        return f"{current} is not a link, so it was left alone; move it away and rerun"
    try:
        _write_atomic(CACHE / "current.txt", f"{data}\n")
    except OSError as e:
        return f"{CACHE / 'current.txt'} could not be written ({e})"
    staged = CACHE / f".current-{os.getpid()}"
    try:
        staged.unlink(missing_ok=True)
        staged.symlink_to(data, target_is_directory=True)
        os.replace(staged, current)  # current is never missing while it moves
    except OSError as e:
        staged.unlink(missing_ok=True)
        current.unlink(missing_ok=True)  # a link, checked above: absent beats naming another dir
        _log(f"warning: {current} could not be made a link to {data} ({e}); {CACHE / 'current.txt'} names it")
        return None
    if Path(os.readlink(current)) != data:
        return f"{current} points at {os.readlink(current)} after updating it, not at {data}"
    return None


def _serve(a: argparse.Namespace, tag: str, data: Path, meta: dict) -> int:
    """Map the unpacked library for remote mode, then point `current` at it and print how to serve it."""
    try:
        _map_data_dir(data)
    except RuntimeError as e:
        _log(f"{tag} is unpacked in {data}, but remote mode cannot play it there: {e}")
        return 2
    problem = _point_current(data)
    if problem:
        _log(f"{tag} is unpacked in {data}, but {problem}")
        return 2
    if meta.get("unanalyzed"):
        _log(f"warning: {tag} has tracks with no rekordbox analysis: {meta['unanalyzed']}")
    if a.print_env:
        # Quoted for eval. The MDT_* lines are the production remote-library mode
        # that serves the pack's bundled ANLZ files from <data>/pioneer-share.
        for k, v in _serve_env(tag, data, meta).items():
            print(f"{k}={shlex.quote(v)}")
    else:
        env = " ".join(f"{k}={shlex.quote(v)}" for k, v in _serve_env(tag, data, meta).items() if k.startswith("MDT_"))
        _log(
            f"{tag}: {len(meta['stable_ids'])} tracks ready; start with {env} python -m apps.engine_core serve --data-dir {shlex.quote(str(data))}"
        )
        print(data)
    return 0


def _serve_env(tag: str, data: Path, meta: dict) -> dict[str, str]:
    return {
        "ODJ_FIXTURE_TAG": tag,
        "ODJ_FIXTURE_DATA_DIR": str(data),
        "ODJ_FIXTURE_IDS": ",".join(meta["stable_ids"]),
        "MDT_DATA_DIR": str(data),
        "MDT_LIBRARY_MODE": "remote",
        "MDT_CRATE_ROOT": str(data.resolve()),
        "MDT_PATH_MAP": str(data.resolve() / PATH_MAP),
    }


def _map_data_dir(data: Path) -> None:
    """Map the unpacked library onto itself in (private path), keeping its other entries.

    The printed env is remote library mode, where the app's resolver refuses
    every Mac ``/Users/...`` path the path map does not rewrite (a leftover home
    tree must not win). The default cache on a Mac is under /Users, so without
    this entry no unpacked track would play there. The entry is the identity, so
    it changes no path anywhere else, and every entry already in the file stays:
    when --data-dir names a library with its own map, that map still applies.
    """
    target, merged, changed, problem = _merged_map(data)
    if problem:
        raise RuntimeError(problem)
    if changed:
        _write_atomic(target, json.dumps(merged, indent=1))


def _merged_map(data: Path) -> tuple[Path, dict, bool, str | None]:
    """((private path), it with the identity entry, whether that differs, why the app would refuse it).

    The app loads the map before it resolves any track and refuses a malformed
    entry, so the merged map is loaded exactly as the app loads it. ensure asks
    this before it moves a library aside, so a map that would fail never costs
    the library it was meant to serve.
    """
    root = str(data.resolve())
    target = data / PATH_MAP
    raw: object = {"entries": []}
    if os.path.lexists(target):
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return target, {}, False, f"{target} is not a path map this script can extend ({e}); fix or remove it"
    if not isinstance(raw, dict) or not isinstance(raw.get("entries", []), list):
        return target, {}, False, f"{target} is not a path map this script can extend; fix or remove it"
    # Any entry for data itself or a path inside it would win over the identity
    # (production takes the longest prefix, then file order), so it is dropped.
    inside = (root + "/", root + "\\")
    keep = [
        e
        for e in raw.get("entries", [])
        if not (
            isinstance(e, dict)
            and isinstance(e.get("from"), str)
            and (e["from"].rstrip("/\\") == root or e["from"].startswith(inside))
        )
    ]
    merged = {**raw, "entries": [{"from": root, "to": root}, *keep]}
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from apps.shared.platform_paths import load_path_map

    saved = os.environ.get("MDT_PATH_MAP")
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / PATH_MAP
        probe.write_text(json.dumps(merged), encoding="utf-8")
        os.environ["MDT_PATH_MAP"] = str(probe)
        try:
            load_path_map()
        except ValueError as e:
            return target, merged, False, f"{target} holds an entry the app refuses ({e}); fix or remove it"
        finally:
            if saved is None:
                os.environ.pop("MDT_PATH_MAP")
            else:
                os.environ["MDT_PATH_MAP"] = saved
    return target, merged, merged != raw, None


def cmd_list(_: argparse.Namespace) -> int:
    for r in _releases():
        z = next((x for x in r["assets"] if x["name"] == ZIP_ASSET), None)
        print(f"{r['tag_name']}  {r['created_at']}  {(z['size'] / 1048576 if z else 0):.0f} MB")
    return 0
