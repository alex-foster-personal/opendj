"""Offline provisioning of the Beat This! checkpoint this lane runs on.

WHY THE LANE OWNS THIS AT ALL. `specs/native-analysis-v1.md` section 4 says
there is no network after install. `beat_this_runner.py` defaults to loading
the checkpoint by the NAME `final0`, and Beat This! resolves a bare name
through Torch Hub, which downloads it on first use into the user's own hub
cache. So registering the runner as a backfill backend does not make the
backfill network-free: a fresh install would reach for the network the first
time a track was analyzed, and fail in a place the user cannot act on.

WHAT THIS MODULE GUARANTEES. The checkpoint is a FILE THIS PROJECT PLACED, at
a path this module resolves, whose sha256 is asserted at load. Two locations
are searched, both named, and there is no third:

1. ``MDT_BEATGRID_WEIGHTS`` -- an absolute path to the checkpoint file. This is
   how a packaged build points at the copy bundled into its own resources, and
   how a test points at a fixture.
2. ``<DATA_DIR>/weights/beatgrid/beat_this-final0.ckpt`` -- the app's own
   weights directory, which ``install`` writes and which a checkout gets by
   running ``python -m apps.analysis_beatgrid.weights install --from <path>``.

There is deliberately NO fall back to the Torch Hub cache and no fall back to
the bare name. Either would restore the exact behavior this module exists to
remove, and it would do so silently on the one machine that has a warm cache:
the developer's. A missing or mismatched file raises, naming both searched
locations and the expected digest, because an analyzer that quietly downloaded
different weights would put a different producer's beats under this producer's
version.

WHY THE DIGEST IS ASSERTED AND NOT MERELY RECORDED. `model_sha256` is part of
every own record (spec section 3) and the cross-host parity gate compares it.
A file at the right path is not evidence it is the right file: it can be
truncated by a failed copy, replaced by a different Beat This! release, or be
some other checkpoint entirely. So the digest is computed on load and compared
against :data:`CHECKPOINT_SHA256`, and the record's `model_sha256` is that
verified value rather than a hopeful constant.

WHAT THIS MODULE DOES NOT DO. It does not provision the INTERPRETER the runner
needs. A packaged build gets both halves from `scripts/payload_beatgrid.py`
(NATIVE-10): the runner's own pinned site plus `bin/opendj-beatgrid-python`
for `MDT_BEATGRID_RUNNER_PYTHON`, and this checkpoint at
`models/beatgrid/` for `MDT_BEATGRID_WEIGHTS`, both exported by the payload
launchers.

-Claude
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

#: The checkpoint file name, kept identical to the one Beat This! itself uses
#: for `final0` so a file copied out of a hub cache needs no renaming.
CHECKPOINT_FILENAME = "beat_this-final0.ckpt"

#: sha256 of the `final0` checkpoint the round-1 measurements were taken on,
#: measured Wed 9 Sep 2026 on this Mac from
#: ~/.cache/torch/hub/checkpoints/beat_this-final0.ckpt (81058141 bytes).
#: Bare hex, no `sha256:` prefix: the prefix belongs to the record field, not
#: to a file digest.
CHECKPOINT_SHA256 = "8c328b45f59d8dd3dff219253ff6a8d6482be57d0133a29140e2febbf8eb8331"

#: Absolute path override, read at call time so a test or a packaged build can
#: set it. Empty or unset means "not provided", never "use the hub cache".
WEIGHTS_PATH_ENV = "MDT_BEATGRID_WEIGHTS"

_READ_BLOCK = 1 << 20


class WeightsError(RuntimeError):
    """The checkpoint this lane must run on is not usable."""


class WeightsUnavailable(WeightsError):
    """No checkpoint file was found at either searched location."""


class WeightsMismatch(WeightsError):
    """A checkpoint file exists but is not the one this producer is pinned to."""


#-----------------------------------------------------------------------------
# locations
#-----------------------------------------------------------------------------

def app_weights_dir() -> Path:
    """The app's own beatgrid weights directory, under the resolved data dir.

    Imported at call time so ``MDT_DATA_DIR`` is honored per call rather than
    frozen at import: a worktree daemon and a test both move it.
    """
    from apps.shared.platform_paths import DATA_DIR

    return DATA_DIR / "weights" / "beatgrid"


def search_paths(*, app_dir: Path | None = None) -> list[Path]:
    """Every location searched, in order. Exactly two, and both are named.

    ``app_dir`` overrides location 2 for a caller that must know exactly which
    directory was consulted (the tests, and a bench run pointed at a staging
    tree). It is a parameter rather than a second environment variable so a
    test cannot be fooled by ``MDT_DATA_DIR`` having been resolved at import.
    """
    paths: list[Path] = []
    override = os.environ.get(WEIGHTS_PATH_ENV, "").strip()
    if override:
        paths.append(Path(override))
    paths.append((app_dir if app_dir is not None else app_weights_dir()) / CHECKPOINT_FILENAME)
    return paths


#-----------------------------------------------------------------------------
# digest
#-----------------------------------------------------------------------------

def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_READ_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


#-----------------------------------------------------------------------------
# resolution
#-----------------------------------------------------------------------------

def resolve_checkpoint(
    *, expected_sha256: str = CHECKPOINT_SHA256, app_dir: Path | None = None
) -> tuple[Path, str]:
    """``(path, sha256)`` of the bundled checkpoint, or raise naming the reason.

    The digest is returned rather than assumed by the caller, so the value a
    record stamps is the one this function actually measured on this machine.

    An explicit ``MDT_BEATGRID_WEIGHTS`` that does not name a file raises
    immediately rather than falling through to location 2 (Sol P1, PR #1587):
    a caller who set it named the exact checkpoint they mean to run on, and a
    silent fall-back to whatever happens to be in the app's own weights
    directory would run a DIFFERENT checkpoint than the one asked for, with no
    error to say so. That is the same silent-substitution failure this module
    exists to prevent, one location earlier.
    """
    searched = search_paths(app_dir=app_dir)
    override = searched[0] if os.environ.get(WEIGHTS_PATH_ENV, "").strip() else None
    if override is not None and not override.is_file():
        raise WeightsUnavailable(
            f"MDT_BEATGRID_WEIGHTS={override} does not name a file; this "
            "producer never falls back to the app weights directory (or the "
            "Torch Hub name) when an explicit override is set and unusable, "
            "because either would run a different checkpoint than the one "
            f"named. Searched {[str(p) for p in searched]}"
        )
    found = next((p for p in searched if p.is_file()), None)
    if found is None:
        raise WeightsUnavailable(
            "no Beat This! checkpoint is provisioned for the beatgrid lane; "
            f"searched {[str(p) for p in searched]}. This producer never falls "
            "back to the Torch Hub name, which would download weights over the "
            "network on a fresh install (specs/native-analysis-v1.md section 4). "
            "Install it with: python -m apps.analysis_beatgrid.weights install "
            "--from <path to " + CHECKPOINT_FILENAME + ">"
        )
    actual = sha256_of(found)
    if actual != expected_sha256:
        raise WeightsMismatch(
            f"checkpoint {found} has sha256 {actual}, but this producer "
            f"({CHECKPOINT_FILENAME}) is pinned to {expected_sha256}; the beats "
            "a different checkpoint emits are a different producer's beats and "
            "must not be written under this producer version"
        )
    return found, actual


#-----------------------------------------------------------------------------
# install
#-----------------------------------------------------------------------------

def install(
    source: Path, *, dest_dir: Path | None = None, expected_sha256: str = CHECKPOINT_SHA256
) -> Path:
    """Copy a VERIFIED checkpoint into the app's weights directory.

    The developer and bench command. The shipping path is the copy bundled into
    the build, not this. The source is verified BEFORE anything is written, so a
    wrong file cannot land at the right path and be discovered only at load.
    """
    if not source.is_file():
        raise WeightsUnavailable(f"source checkpoint {source} does not exist")
    actual = sha256_of(source)
    if actual != expected_sha256:
        raise WeightsMismatch(
            f"refusing to install {source}: sha256 {actual} != expected "
            f"{expected_sha256}"
        )
    target_dir = dest_dir if dest_dir is not None else app_weights_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / CHECKPOINT_FILENAME
    # Copy to a sibling then rename: an interrupted copy must not leave a
    # truncated file at the resolved path, where the next run would meet it as
    # a digest mismatch rather than as an absent install.
    staging = target_dir / (CHECKPOINT_FILENAME + ".partial")
    shutil.copyfile(source, staging)
    staged = sha256_of(staging)
    if staged != expected_sha256:
        staging.unlink(missing_ok=True)
        raise WeightsMismatch(
            f"copy of {source} landed with sha256 {staged}, not {expected_sha256}; "
            "nothing was installed"
        )
    staging.replace(target)
    return target


#-----------------------------------------------------------------------------
# CLI
#-----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m apps.analysis_beatgrid.weights",
        description=(
            "Provision the Beat This! checkpoint for the beatgrid backfill. "
            "Developer and bench command; a shipped build carries the same "
            "verified file in its own resources."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    installer = sub.add_parser("install", help="copy a verified checkpoint into place")
    installer.add_argument("--from", dest="source", required=True, type=Path)
    installer.add_argument("--dest-dir", type=Path, default=None)
    sub.add_parser("verify", help="resolve and verify the installed checkpoint")
    sub.add_parser("path", help="print every location searched, in order")
    args = parser.parse_args(argv)

    if args.command == "path":
        for candidate in search_paths():
            print(f"{candidate} {'[present]' if candidate.is_file() else '[absent]'}")
        return 0
    if args.command == "install":
        target = install(args.source, dest_dir=args.dest_dir)
        print(f"[weights] installed {target} sha256 {CHECKPOINT_SHA256}")
        return 0
    path, digest = resolve_checkpoint()
    print(f"[weights] {path} sha256 {digest} OK")
    return 0


__all__ = [
    "CHECKPOINT_FILENAME",
    "CHECKPOINT_SHA256",
    "WEIGHTS_PATH_ENV",
    "WeightsError",
    "WeightsMismatch",
    "WeightsUnavailable",
    "app_weights_dir",
    "install",
    "resolve_checkpoint",
    "search_paths",
    "sha256_of",
]


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    sys.exit(main())
