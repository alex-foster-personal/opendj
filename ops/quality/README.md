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

The single exception is `arch.contracts_broken`, which is hard-gated at zero
regardless of baseline. An architecture rule with a growing allowance is not a
rule. Its debt is carried instead as a named, dated list of exact imports in
[`.importlinter`](../../.importlinter), so you can read the debt rather than
just counting it.

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
