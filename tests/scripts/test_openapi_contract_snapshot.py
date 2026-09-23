"""The committed OpenAPI contract is the file `just openapi-dump` writes, not a hand edit.

Two trunk incidents made this a guard: Sat 19 Sep 2026 main's committed
apps/webui/openapi.json had been truncated from 286 paths to 134, and Tue 22 Sep
2026 PR #3790 merged with 260 of 293 paths, unsorted, generated from something
other than the dump recipe. Both turned the contract-drift job red on EVERY PR
(it checks out the merge ref), and both would have failed here in the fast tier
before merge. The drift job stays the arbiter of CONTENT; this pins the SHAPE
the dump recipe guarantees, without importing the app.

Requirements (mini-PRD)
- [if] the committed file is byte-for-byte `json.dumps(obj, indent=2, sort_keys=True)`
  plus one trailing newline (what `engine_openapi_dump` in the justfile writes)
  [then] pass, [else] fail naming the first differing line ✔︎ ✅ 🎯
- [if] the committed file has fewer than MIN_PATHS paths [then] fail naming the count:
  removing endpoints is a deliberate act that lowers the floor in the same PR ✔︎ ✅ 🎯
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPENAPI = ROOT / "apps" / "webui" / "openapi.json"
# 293 on Tue 22 Sep 2026. A floor, not a pin: lower it deliberately when endpoints go.
MIN_PATHS = 280


def test_the_committed_contract_is_the_dump_recipe_s_canonical_form() -> None:
    text = OPENAPI.read_text(encoding="utf-8")
    canonical = json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"
    if text != canonical:
        for number, (have, want) in enumerate(
            zip(text.splitlines(), canonical.splitlines(), strict=False), start=1
        ):
            if have != want:
                raise AssertionError(
                    f"apps/webui/openapi.json is not the sorted, 2-space dump the justfile "
                    f"recipe writes; first difference at line {number}: {have!r} != {want!r}. "
                    "Regenerate it with `just openapi-dump` (or `just pre-push`)."
                )
        raise AssertionError("apps/webui/openapi.json differs from its canonical form in length")


def test_the_committed_contract_is_not_truncated() -> None:
    paths = json.loads(OPENAPI.read_text(encoding="utf-8"))["paths"]
    assert len(paths) >= MIN_PATHS, (
        f"apps/webui/openapi.json has {len(paths)} paths, below the {MIN_PATHS} floor: "
        "either the dump ran against a partial app (Sat 19 Sep: 134, Tue 22 Sep: 260) "
        "or endpoints were removed on purpose, in which case lower MIN_PATHS in this PR."
    )
