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

`shell.files_scanned` and `mypy.files_checked` are report-only for a third
reason: they are **controls**, not allowances. An error count falls when the
code improves and it falls when the gate stops looking, and only the count of
things looked at tells those two apart. Gating a control is wrong in both
directions (a ratchet fails on every module added; a hard zero is nonsense), so
each instead carries a floor in its evaluator that ABORTS the run rather than
scoring a collapsed scan: `shell_construct_lint.CFG.MIN_FILES` and
`CFG.MYPY_MIN_FILES`.

## A regression that main already carries is not yours

A merge can land a metric over its allowance that NEITHER parent exceeded:
two branches each add lines to the same file, each stays under the limit, and
the union crosses it. Once that sits on main, every later PR measures the same
over-allowance metric and would fail on a regression it inherited (issue
#1155, worked instance: `file_size.over_limit_python` 49 -> 50 on merged
line-adds to one file).

So when a metric regresses, the gate re-measures that metric on the merge-base
`git merge-base HEAD origin/main` and splits the outcome:

- main is AT OR ABOVE this run's value: the line prints `INHERITED`, names
  main as the owner (`main (sha) is ALSO at N - this is a trunk regression,
  not yours`), and does not fail the run. Seven PR authors should not each
  debug the same trunk state.
- main is BELOW this run's value: the change made an already-bad number
  worse, so it stays a hard `REGRESSION` and fails. Only `base >= run` is
  inherited; `base` at 50 and the run at 51 is the run's fault.
- the merge base cannot be measured (HEAD is itself on main, the ref is
  missing, or the base tree will not run): the message is unchanged and the
  output says why. The gate never downgrades on a guess and never passes
  silently.

The base re-measure only runs when something actually regressed, and only for
the evaluator that owns the regressed metric. It checks the base out with
`git worktree add --detach` into a throwaway tempdir and runs that commit's
own committed copy of the gate there, so a metric over `scripts/` is measured
against the base's version of the file and a branch-only flag does not have to
exist on main for the measurement to work. Hard-zero rules are never offered
the downgrade: a broken architecture contract has no allowance to be "over"
on main.

## The mypy ratchet, and its pinned install set

Type debt was completely unmeasured until Tue 1 Sep 2026. The repo had no
`[tool.mypy]` section at all, so nothing type-checked anything: 553 errors at
default strictness over `apps` + `tests`, against 119 measured in April. The
number had quadrupled with no gate able to see it. (Both of those figures were
taken with a project venv in scope, so they are history rather than something to
compare against `baseline.json`; see the isolation note below.)

`mypy.errors_apps` (157), `mypy.errors_tests` (215) and `mypy.errors_scripts`
(106) are ordinary ratchets recorded at today's values, so nothing is red on day
one and the pile can only shrink from here. Nothing was fixed in the PR that landed them; the
point was to turn an unmeasured quantity into a measured one.

The one design decision worth knowing about is **where the count is measured**.
mypy is the only tool in this gate whose answer depends on what else is
importable from the environment it runs in, because an unresolvable third-party
import scores as an error. Measured against a `uv sync`, the number would move
whenever an optional extra did or did not resolve on the host -- exactly the
`deps.issues` failure mode above.

So mypy gets its own throwaway environment holding **mypy and nothing else**,
pinned in `ops/quality/mypy-requirements.txt` (separate from the shared
`requirements.txt` so a ruff or deptry bump dragging in a transitive dependency
cannot move the type count). Which third-party imports are silenced is then a
property of the source tree: the `[[tool.mypy.overrides]]` block in
`pyproject.toml` lists them, and it takes **1356 errors to 478**. The 29 import
errors that survive are all first-party modules that only resolve through
`sys.path` manipulation at runtime, plus the compiled Rust extension; those are
findings, not environment artifacts, so they stay in the count.
`python_version`, `platform` and `exclude` are pinned in the same block for the
same reason; the header comment there explains each one.

### "Nothing else" needed `--isolated`, and that cost a red CI run

`uv run --no-project` is not enough, which is worth writing down because it is
not what the flag sounds like. uv declines to sync the project, then still
discovers a `.venv` in the working directory and layers the
`--with-requirements` packages **on top of it**. So the first recording of
these metrics was taken with the whole project venv silently in scope: 550
errors on a developer worktree, against 1356 in CI, which has no venv, for one
identical commit. CI caught it the same day by reading a number no local run
could reproduce.

`--isolated` is what actually empties the environment. Verified by measuring the
same commit in a worktree with a `.venv` and in one without: 157 / 215 / 106
both times. `tests/test_quality_gate.py` now fails if that flag is dropped.

The same leak applies in principle to any tool here that resolves imports.
ruff and radon parse rather than resolve, and import-linter scores only
first-party modules, so they cannot see a venv at all. deptry can, and
`deps.issues` is already report-only for exactly this class of reason; changing
its environment would move that number for reasons unrelated to the tree, so it
is left alone deliberately rather than by oversight.

### What the count does NOT yet see

Everything this repo depends on is silenced, including its typed dependencies:
`pydantic`, `fastapi`, `pytest`, `numpy`, `sqlalchemy`. A mistyped pydantic
field is therefore invisible to the gate today. That is a deliberate trade of
fidelity for reproducibility, and it is reversible one package at a time: add an
exact pin to `ops/quality/mypy-requirements.txt` (that file IS the install set),
delete its line from the overrides block, and reprint the baseline in the same
diff. That is the one legitimate reason to RAISE these allowances, and it has
to say so in the diff.

Two more things to know:

- `just typecheck` runs the isolated environment, so it prints the number the
  gate uses. It exits non-zero while any debt remains, which is mypy reporting
  rather than the recipe breaking. `just quality-types` is the gated form.
- The scope lives in `[tool.mypy] files` and nowhere else. The gate passes mypy
  no paths at all, and `tests/quality/test_mypy_scope.py` fails if that list
  drifts from the set ruff lints.

## Allowances raised by hand, and why

`ruff.complexity` carries one hand-raised point from Mon 31 Aug 2026 (round 4's
cloudsync work), on top of the entries below. All were raised deliberately in
the diff that found them, and each is a debt with a named owner rather than a
number that drifted.

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

Six further entries in `baseline.json` predate this branch and are above where
they once stood. All were raised deliberately, each in the diff that caused it,
and each is a debt with a named owner. The two `crate_sync.py` rows date from
Sun 17 Aug 2026; two frontend rows from Sat 29 Aug 2026, and two more from
Mon 31 Aug 2026, each described in the subsections below.

CI was fail-open between the default-branch rename and PR #464, so the
`fix(agentbox)` crate-reconcile train (`6287d7c1`, `58dbfc13`, `bfa33dec`,
`5dc2664c`) landed nine new complexity findings unmeasured. Five of them are
paid back in the same PR as this note. These two are not:

| metric                         | was | now | what is in the gap                                                                                                                                                                                                         |
| ------------------------------ | --- | --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ruff.complexity`              | 140 | 144 | `apps/webui/crate_sync.py::_run`, which `6287d7c1`, `58dbfc13` and `5dc2664c` grew until it tripped four rules at once: C901 (18 > 12), PLR0911 (9 returns > 8), PLR0912 (18 branches > 15), PLR0915 (96 statements > 60). |
| `complexity.blocks_over_limit` | 142 | 143 | `apps/webui/crate_sync.py::_rsync_manifest_from_host`, added by `6287d7c1` over the mccabe limit of 12.                                                                                                                    |

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

### Sat 29 Aug 2026: the cost of a fourth hand-written device map

Adding `src/lib/rb/midi/maps/ddj-400.ts` (PR #508, the DDJ-400 map rescue)
moved two frontend numbers. Both are the per-device TypeScript map file itself,
not anything the map does.

| metric                | was  | now  | what is in the gap                                                                                                                                                                                                                                                                                                                                                                       |
| --------------------- | ---- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `frontend.max_fan_in` | 52   | 53   | One more importer of `src/lib/rb/types.ts`. Every device map imports `DeckId` and `HotCueSlot` from it, so each new controller costs exactly +1 here.                                                                                                                                                                                                                                    |
| `duplication.percent` | 0.29 | 0.32 | Two 41-line jscpd clones between `maps/ddj-400.ts` and `maps/ddj-flx10.ts` (at `:96 <-> :100` and `:136 <-> :138`). They are the shared per-deck binding-builder scaffolding every Pioneer map repeats: same `_deckCh` / `_note` / per-deck array shape, different wire numbers and different PDF citations. jscpd normalizes literals, so the differing numbers do not break the match. |

Refactoring the scaffolding into a shared helper would have meant editing the
hardware-verified FLX10 map inside a rescue PR whose whole value is that each
file is a self-contained, citable transcription of a vendor PDF. That trade was
not worth making for 0.03%.

Paying this down is not a refactor of these two files, it is the runtime device
document in `specs/controller-onboarding.md` section 3.1. Once a controller is
JSON data plus provenance rather than a fourth copy of the same builder, both
numbers fall and neither grows again per device. Re-record them the moment that
lands, and do not raise either again without adding a row above.

### Mon 31 Aug 2026: extracting a picker out of BrowserPanel

PR #559 rescues a Mon 17 Aug 2026 fix for a double-click load race that had
lived only on the Air. The fix pulls the deck-target picker out of
`BrowserPanel.svelte` into a pure `src/lib/rb/double-click-deck-pick.ts` so the
race is testable without a component harness, and it arrives with a 112-line
unit test. Both numbers moved because of the extraction itself, not because of
anything the picker does.

| metric                 | was | now | what is in the gap                                                                                                                                                  |
| ---------------------- | --- | --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `frontend.max_fan_in`  | 53  | 54  | One more importer of `src/lib/rb/types.ts`. The new module takes `DeckId` from it, the same +1 every device map costs (see the row above).                          |
| `frontend.max_fan_out` | 34  | 35  | One more import in `src/lib/components/rb/BrowserPanel.svelte`, already the worst offender: `import { pickDoubleClickDeck } from '$lib/rb/double-click-deck-pick'`. |

Worth naming plainly: this pair of metrics scored a decoupling as a
regression. The extraction moved 20 lines of untested inline logic into a pure
module with tests, so the component got smaller and easier to change, and the
counters went up because the file count went up. The alternative that keeps
both numbers flat is leaving the picker inline and untestable, which is the
worse tree.

Paying this down is the same work already named above: `max_fan_in` is a
property of `types.ts` being a barrel that every deck-aware module imports, and
falls when that is split by concern rather than by being a single types file.
`max_fan_out` falls when `BrowserPanel.svelte` (4000+ lines, the standing
hotspot) is decomposed. Neither is a job for a rescue PR. Do not raise either
again without adding a row here.

## What each evaluator answers

| evaluator    | tool                        | the question it answers                                                                                                                                                              |
| ------------ | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `ruff`       | ruff (pinned)               | How much lint debt, split by _intent_: complexity, coupling, safety, correctness, style. A rising `ruff.safety` means new blind excepts, which is the fail-fast house rule breaking. |
| `complexity` | radon                       | Worst cyclomatic block, how many blocks exceed the mccabe limit, how many files sit below maintainability rank A.                                                                    |
| `arch`       | import-linter + grimp       | Do the layering contracts hold, and how many top-level packages import each other in a cycle.                                                                                        |
| `deps`       | deptry                      | Imports with no declaration, declarations nothing imports, and imports that only work because something else happened to pull the library in.                                        |
| `frontend`   | knip + a local import graph | Import cycles across `.ts` **and** `.svelte`, worst fan-in and fan-out, orphaned modules, unreferenced exports and packages, and `as unknown as` double-casts (the one assertion the compiler cannot check).                                                         |
| `size`       | local + jscpd               | Longest file per language, count over the review threshold, percentage of duplicated lines.                                                                                          |
| `mypy`       | mypy (pinned, own env)      | How many type errors in `apps/`, in `tests/` and in `scripts/`, plus how many modules were actually checked. Split three ways so test-fixture debt cannot mask a production regression. |

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

`ops/quality/requirements.txt` pins every Python tool to an exact version,
`ops/quality/mypy-requirements.txt` does the same for mypy in an environment of
its own, and `CFG.KNIP` / `CFG.JSCPD` in `scripts/quality_gate.py` do the same
for the node tools. A ratchet compares a number today against a number from last month, so
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
