"""Build a complete-content checksum manifest for a committed USB fixture tree.

Unlike ``tests/fixtures/rb-usb-export.manifest.json`` (which locks only the
one canonical ANLZ directory the native-waveform acceptance gate compares
against), the "contract" manifest this script writes covers EVERY file in
the fixture tree: OneLibrary databases, artwork, every ANLZ directory, and
so on. ``resolve_required_fixture()`` (``tests/fixtures/conftest.py``)
verifies against it so a stale, regenerated, or partially copied
``MUX_FIXTURE_HOST`` tree is rejected rather than silently accepted
(AGENTS.md: "Verify canonical fixtures by version, manifest, and checksum
before use").

The script only ever READS the fixture tree; it never mutates it. Usage::

    python -m scripts.make_usb_fixture_contract rb-usb-export
    python -m scripts.make_usb_fixture_contract rb-usb-export-onetera-20260805
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TypedDict

from apps.shared.hashing import sha256_file
from tests.fixtures._resolver import fixture_path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures"


class FixtureContract(TypedDict):
    contract_version: int
    fixture: str
    files: dict[str, str]


def build_contract(name: str) -> FixtureContract:
    # Routes through the resolver (rather than a hard-coded FIXTURES_ROOT /
    # name check) so this generator keeps working once a fixture is fully
    # externalized -- an in-repo directory, an ad-hoc symlink, and an
    # ``.extern``-marker-resolved external tree (mounted or via
    # MUX_FIXTURE_HOST) all resolve the same way (PR #718 review).
    root = fixture_path(name)
    files = {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    return {"contract_version": 1, "fixture": name, "files": files}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="Fixture directory name under tests/fixtures/")
    args = parser.parse_args()

    contract = build_contract(args.name)
    out_path = FIXTURES_ROOT / f"{args.name}.contract.json"
    out_path.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out_path} ({len(contract['files'])} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
