# Code quality evaluators

```bash
just quality
```

Scores the tree, writes `ops/quality/report.md`, and exits non-zero if anything
got worse than `ops/quality/baseline.json` allows.

## The one design decision

This is a **ratchet**, not a standard. Nothing is required to be clean; things
are required not to get worse. The tree currently carries 2825 lint findings,
141 functions over the complexity limit, and a 4556-line frontend module. A
gate that demanded zero would be switched off within a day, and a gate that is
switched off measures nothing.

So every metric has a recorded allowance. Exceed it and the build fails. Come
in under it and the run prints `RATCHET AVAILABLE`, which you bank with:

```bash
just quality-baseline    # rewrites baseline.json at today's numbers
```

Allowances only ever shrink. Raising one by hand is a decision someone has to
defend in a diff, which is the point.

There are two exceptions, in opposite directions.

`arch.contracts_broken` is **hard-gated at zero** regardless of baseline. An
architecture rule with a growing allowance is not a rule. Its debt is carried
instead as a named, dated list of exact imports in
[`.importlinter`](../../.importlinter), so you can read the debt rather than
just counting it.

`deps.issues` is **report only**: measured and printed every run, never gated,
and deliberately absent from `baseline.json` (that file lists allowances, and a
number nothing is allowed to exceed is not one). deptry's answer depends on the
host, not just the tree. It splits an import into DEP001 (undeclared) or DEP003
(transitive) by what it can actually resolve, and scores a platform-gated
declaration as DEP002 (unused) on the platform where the marker is false.
Measured Mon 17 Aug 2026 on an identical tree: macOS read 22 (DEP003=12,
DEP001=6, DEP002=4), ubuntu-latest read 24 (DEP001=19, DEP002=5, the extra
DEP002 being `pyobjc-framework-Quartz` behind `sys_platform == 'darwin'`). The
other 22 metrics agreed exactly across both hosts, so this is deptry's
environment sensitivity rather than noise in the gate.

A ratchet compares today's number against one recorded on someone else's
machine, so a host-dependent metric can only produce false failures or an
allowance quietly inflated to the worst platform. Both end with the gate being
switched off. To re-gate it, make the measurement host-independent (run deptry
in a pinned container, or record a per-platform baseline) and then remove it
from `REPORT_ONLY` in `scripts/quality_gate.py` -- do not just delete the entry.

## Allowances raised by hand, and why

`ruff.complexity` carries one hand-raised point from Mon 31 Aug 2026, on top
of the two below from Sun 17 Aug 2026. All three were raised deliberately in
the diff that found them, and all three are debts with a named owner rather
than a number that drifted.

Round 4's quality-gate ratchet (`.planning/cloudsync-round3-adversarial.md`
Part 3 item 8) traced round 3's cloudsync work pushing `ruff.complexity` from
144 to 145 to one line: `apps/shared/state/locations.py:231`,
`upsert_location`, PLR0913 (9 args > 8). N1b (round 2) fixed
`hydration.py` writing a NULL `machine_id`, and the row's owner has to travel
through the same call as the rest of the natural key -- the ninth parameter is
`machine_id`. Bundling parameters to dodge the threshold would touch four call
sites, two of them test modules owned by other round 4 lanes, for a signature
`specs/design_decision_08.md` point 1 already documents. See
`baseline.json`'s `CLOUDSYNC_MACHINE_SCOPE` entry. Paid back only if
`upsert_location` grows a tenth parameter.

Two further entries in `baseline.json` are above where they stood on
Sun 17 Aug 2026.

CI was fail-open between the default-branch rename and PR #464, so the
`fix(agentbox)` crate-reconcile train (`6287d7c1`, `58dbfc13`, `bfa33dec`,
`5dc2664c`) landed nine new complexity findings unmeasured. Five of them are
paid back in the same PR as this note. These two are not:

| metric | was | now | what is in the gap |
| ------ | --- | --- | ------------------ |
| `ruff.complexity` | 140 | 144 | `apps/webui/crate_sync.py::_run`, which `6287d7c1`, `58dbfc13` and `5dc2664c` grew until it tripped four rules at once: C901 (18 > 12), PLR0911 (9 returns > 8), PLR0912 (18 branches > 15), PLR0915 (96 statements > 60). |
| `complexity.blocks_over_limit` | 142 | 143 | `apps/webui/crate_sync.py::_rsync_manifest_from_host`, added by `6287d7c1` over the mccabe limit of 12. |

Both live in `apps/webui/crate_sync.py`, which was under concurrent edit by the
Windows parity lane when this was recovered, so refactoring it here would have
collided with live work rather than fixed anything. That file is already the
worst maintainability score in the tree (`complexity.low_maintainability_files`
names it at MI 0.0), and it grew from 910 to 1556 lines across that same train.
It is the single highest-value Python refactor available right now.

Paying this down means splitting `_run` into per-subcommand handlers and
lifting the manifest fetch out of `_rsync_manifest_from_host`. Doing that takes
`ruff.complexity` back to 140 and `complexity.blocks_over_limit` to 142, and
both should be re-recorded the moment it lands. Do not raise either number
again without adding a row above.

## What each evaluator answers

| evaluator | tool | the question it answers |
| --------- | ---- | ----------------------- |
| `ruff` | ruff (pinned) | How much lint debt, split by *intent*: complexity, coupling, safety, correctness, style. A rising `ruff.safety` means new blind excepts, which is the fail-fast house rule breaking. |
| `complexity` | radon | Worst cyclomatic block, how many blocks exceed the mccabe limit, how many files sit below maintainability rank A. |
| `arch` | import-linter + grimp | Do the layering contracts hold, and how many top-level packages import each other in a cycle. |
| `deps` | deptry | Imports with no declaration, declarations nothing imports, and imports that only work because something else happened to pull the library in. |
| `frontend` | knip + a local import graph | Import cycles across `.ts` **and** `.svelte`, worst fan-in and fan-out, orphaned modules, unreferenced exports and packages. |
| `size` | local + jscpd | Longest file per language, count over the review threshold, percentage of duplicated lines. |

Plus a **hotspot** table in the report: git churn multiplied by file size over
90 days, never gated. A 2000-line file nobody touches is not urgent; a
600-line file edited 40 times is where refactoring actually pays.

## Why the frontend graph is hand-rolled

`madge` is the obvious off-the-shelf choice and it was tried first. On this
repo it reported `No circular dependency found` while having extracted **zero**
imports: it does not parse import statements out of `.svelte` files, and it
does not resolve SvelteKit's `$lib/` alias, which is how essentially every
import in this codebase is written. It found no cycles because it had built an
empty graph.

The replacement lives in `scripts/quality_gate.py` (`_fe_resolve`, `_fe_graph`,
`_strongly_connected`) and found a genuine cycle on its first run:
`performance-ipc.svelte.ts <-> performance-preset.ts`. Because it is our code
rather than a vendor's, it carries regression tests in
`tests/test_quality_gate.py`, including one that fails if the graph ever goes
empty again.

## Pins, and why they are exact

`ops/quality/requirements.txt` pins every Python tool to an exact version, and
`CFG.KNIP` / `CFG.JSCPD` in `scripts/quality_gate.py` do the same for the node
tools. A ratchet compares a number today against a number from last month, so
the measurement has to be identical. Ruff in particular widened its default
rule set substantially between 0.5 and 0.16; before this directory existed, the
repo had no `[tool.ruff]` section at all and `make lint` inherited whichever
defaults the local install shipped. Two developers could disagree about whether
the tree was clean and both be right.

Bumping a pin is fine. It is a deliberate act, and the same commit should
re-record the baseline, because the new tool measures a different thing.

Tools run through `uv run --no-project --with-requirements`, in a throwaway
environment. A quality run never touches `.venv` and never depends on whether
an optional extra resolved.

## Current debt, as of Sun 17 Aug 2026

Highest-value targets, in the order they are worth doing:

1. **Five imports create three package cycles.** `apps.smartlists`,
   `apps.stems` and `apps.vocals` all import back into `apps.webui`, reaching
   only `webui.server.playlist_writeback` and `webui.server.stem_artifacts`.
   Neither module holds HTTP concerns. Move both down into a domain package and
   all three cycles disappear at once.
2. **`apps/sync/usb/pioneer/differ.py` imports `tests`** at two call sites.
   `tests/*` is excluded from the wheel, so this is an `ImportError` waiting
   for the first user who is not running from a source checkout.
3. **18 orphaned frontend modules**, including `lib/rb/knob-control.svelte.ts`,
   which is complete, sophisticated, and imported by nothing.
4. **136 blind `except Exception`** handlers, against a house rule that says
   fail fast with explicit errors and no silent handling. Concentrated in
   `apps/tags/collect.py` (10), `apps/analysis/backends/librosa_madmom.py` (6),
   `apps/cloud/replicate.py` (5), `apps/sync/apply_ratings.py` (5) and
   `apps/sync/rb_writer.py` (5).
5. **Six imports that only work by accident**: `starlette` in three webui
   modules, `joblib` in two `apps/sets/classify` modules, `madmom` in the
   analysis backend. All are transitive: they resolve today because fastapi,
   scikit-learn and librosa happen to pull them in, and break the day any of
   those repins or vendors.
6. **`Actuator.execute_action` at cyclomatic complexity 44** and `usb
   preflight` at 41, against a limit of 12. Two files sit at maintainability
   rank C: `apps/webui/crate_sync.py` (MI 5.0) and `apps/vocals/cli.py`
   (MI 6.1), on a scale where anything under 20 is hard to change safely.
