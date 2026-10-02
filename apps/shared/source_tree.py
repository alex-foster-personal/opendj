"""Where the engine may write: never under its own source tree.

In the signed desktop app the engine's source tree is
``Open DJ.app/Contents/Resources/payload/app`` -- what ``PROJECT_ROOT`` and
every ``Path(__file__).parents[N]`` resolve to. A single file added under it
breaks the bundle's code signature (``codesign --verify --deep --strict``
reports "file added"), so runtime state goes under the data dir
(``apps.shared.platform_paths.DATA_DIR``, which the engine sets from
``--data-dir``) and dev-only ledgers that live in the repo refuse to write
outside a repo checkout.

Two kinds of writer, two rules:

* runtime state (voice settings and logs): resolve under ``DATA_DIR`` at call
  time. In a checkout ``DATA_DIR`` is ``<repo>/data`` unless ``MDT_DATA_DIR``
  overrides it, so development paths do not move.
* repo-tracked dev ledgers (``data/progress-tree.yaml``,
  ``scripts/bench/ratings/``): developers commit these files, so the writer
  keeps its repo path in a checkout and refuses in the packaged app, before
  touching disk, with :data:`DEV_ONLY_CODE` and a sentence naming why.

Requirements (INSTALL-30):
  [if] the engine runs from the packaged payload [then] no route writes under
    the payload's source tree [⛔️ if codesign reports a file added]
  [if] the source root is a git checkout and no payload manifest is exported
    [then] the dev-only ledgers write where they always did [⛔️ if dev moves]
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

#: Exported by every payload launcher (scripts/build_engine_payload.py);
#: absent means "running from a repo checkout".
PAYLOAD_MANIFEST_ENV: str = "OPENDJ_PAYLOAD_MANIFEST"

#: Wire code for a dev-only write refused outside a repo checkout.
DEV_ONLY_CODE: str = "dev_only_in_packaged_app"


def is_repo_checkout(
    source_root: Path, *, environ: Mapping[str, str] | None = None
) -> bool:
    """True only when ``source_root`` is a development checkout.

    Both halves are required. The launcher's manifest variable says "this is
    the payload" even when someone runs it from a copied tree, and a missing
    ``.git`` (a file in a worktree, a directory in a clone) says "nobody can
    commit what is written here" even when the payload was launched some
    other way. Either one alone would let a write into the signed bundle
    through.
    """
    env = os.environ if environ is None else environ
    if str(env.get(PAYLOAD_MANIFEST_ENV, "")).strip():
        return False
    return (Path(source_root) / ".git").exists()


def dev_only_refusal(what: str, target: Path) -> dict[str, str]:
    """The 503 detail body a dev-only writer returns outside a checkout."""
    return {
        "code": DEV_ONLY_CODE,
        "message": (
            f"{what} is a development ledger committed to the Open DJ repo "
            f"({target}); this engine is not running from a repo checkout, so "
            "it refuses rather than write into the app's own files. Run it "
            "from a checkout of the repo instead."
        ),
    }


__all__ = [
    "DEV_ONLY_CODE",
    "PAYLOAD_MANIFEST_ENV",
    "dev_only_refusal",
    "is_repo_checkout",
]
