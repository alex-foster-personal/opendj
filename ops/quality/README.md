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

`latency.input_to_applied_ms` is a **declared ceiling** the latency evaluator
echoes from `baseline.json`, not a live p99 measured in CI (there is no
AudioContext on the builder). A reported 0 would be a lie and would ratchet the
allowance to 0 on the next `--update-baseline`. If the EQ apply wiring checked
by `scripts/quality_latency.py` is removed, the evaluator emits 999 and the gate
fails. Do not treat a `RATCHET AVAILABLE` print on this key as an instruction
to go measure actual latency.

A run is judged against **allowance + slack**, where slack is a small declared
band per metric. The section below says why, and what each band is worth.

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

## Slack: one PR's worth of headroom, declared and justified

A ratchet whose allowance is re-recorded at whatever each landing PR achieved
has **zero headroom by construction**: burning debt down does not create slack,
because the wall moves with you. #1200 took `file_size.over_limit_python` from
49 to 48, the allowance followed to 48, and the next PR still started at zero
and still failed at 49 > 48.

Measured at pinned main on Fri 5 Sep 2026 (issue #1219), all seven count
metrics sat at zero headroom **simultaneously**: `ruff.complexity` 142/142,
`complexity.blocks_over_limit` 144/144, `frontend.max_fan_out` 35/35,
`frontend.unknown_casts` 16/16, `file_size.over_limit_python` 48/48,
`file_size.max_frontend` 4042/4042, `file_size.over_limit_frontend` 10/10. Any
PR adding one unit on any one axis was red. Of the 30 red PRs open that
morning, the three that were mergeable-with-a-real-failure failed on this gate
and nothing else (#383, #1122, #1215), and fixers spent hours moving code
between files to fit rather than doing the work in the PR.

So `baseline.json` carries a `slack` block: a per-metric constant worth roughly
one ordinary PR. The gate fails on `measured > allowance + slack`, and a run
inside the band prints

```
[quality] WITHIN SLACK      file_size.over_limit_python: 49 > 48 allowed, inside the 1 slack (ceiling 49)
```

with the metric line marked `~~` rather than held, and the report's status
column reading `within slack`. Nothing is hidden behind the pass.

Four properties make this a budget rather than a hole:

1. **Allowances still only shrink, and `--update-baseline` enforces it.** Slack
   is headroom at gate time, never a recorded number: a rewrite records a
   measurement only when it is LOWER than the allowance already on file, and a
   metric floating inside its band keeps the old number and prints
   `ALLOWANCE KEPT  file_size.over_limit_python: measured 49, allowance kept at
   48`. Without that clamp the band compounds -- the run at 49 passes, the
   update banks 49, the next run passes at 50, and the ceiling walks up one
   band per update while this file still claims allowances only ever shrink.
   Raising one is still possible and still exactly as visible as before: edit
   the number by hand, in a diff someone reviews, with a `burn_down` entry
   saying why.
2. **A missing key is zero, not a default.** A metric with no `slack` entry is
   gated exactly as it was before this existed, so the band can never widen a
   gate nobody wrote down. `tests/quality/test_gate_slack.py` fails if it does.
3. **The band is declared in the repo and reviewed in a diff**, like every
   other allowance here. Raising one is still a decision someone defends.
4. **The ceiling is a ceiling.** `allowance + slack + 1` is red, and a run
   above a trunk that is itself over allowance stays a hard `REGRESSION` (the
   #1155 inheritance rules below are unchanged).

The bands, and why each is that size:

| metric                                 | slack | one PR's worth means                                                                                                                          |
| -------------------------------------- | ----: | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `ruff.total`                           |     8 | The sum of the five bucket bands below. `ruff.total` IS that sum (2192 = 142+64+346+353+1287), so a smaller number would make bucket slack unusable: the PR that spends `ruff.complexity`'s band would fail on the total instead. |
| `ruff.complexity`                      |     2 | One new function over a threshold usually trips more than one rule at once: the worked case in this file (`crate_sync.py::_run`) tripped four. Two covers the ordinary C901-plus-PLR0912 pair without covering a four-rule monster. |
| `ruff.coupling`                        |     1 | One new cross-module import in a feature.                                                                                                       |
| `ruff.style`                           |     5 | A new module of ordinary size lands a handful of style findings before anyone reads it.                                                          |
| `ruff.safety`                          |     0 | Deliberate. A rising `ruff.safety` is a new blind `except Exception`, which is the fail-fast house rule breaking. There is no ordinary PR that needs one. |
| `ruff.correctness`                     |     0 | Deliberate, same reasoning: a new correctness finding is a defect, not debt.                                                                     |
| `complexity.blocks_over_limit`         |     2 | Paired with `ruff.complexity`: one added function over the mccabe limit, plus one for a helper extracted alongside it.                            |
| `complexity.worst_block`               |     0 | A new deepest function in the tree is a review conversation, not a budget line.                                                                  |
| `complexity.low_maintainability_files` |     0 | Same: a whole file dropping below rank A is a decision, not drift.                                                                               |
| `python.package_cycles`                |     0 | A new package cycle is an architecture defect; `arch.contracts_broken` is already hard-gated for the same reason.                                 |
| `mypy.errors_apps` / `_tests` / `_scripts` | 5 each | These counts were recorded as-measured and never fixed (see the mypy section), and a new module lands a few unannotated signatures. 5 is about 3% of each pile, small enough that a real regression still shows. |
| `frontend.import_cycles`               |     0 | A new cycle is the exact defect the hand-rolled graph exists to catch.                                                                            |
| `frontend.max_fan_in`                  |     1 | Every new deck-aware module imports `DeckId` from `types.ts`, so ordinary work costs exactly +1 here (two rows above document this happening).    |
| `frontend.max_fan_out`                 |     1 | One import added to the worst-offender component, which is what extracting a helper out of `BrowserPanel.svelte` costs (the row above documents that PR scoring a decoupling as a regression). |
| `frontend.unknown_casts`               |     1 | One `as unknown as` in new code, which must then be paid back: the `FRONTEND_UNKNOWN_CASTS` burn-down still targets 0 and still names every file. |
| `frontend.unused_exports`              |     3 | A new module usually exports a little more than its first caller consumes.                                                                        |
| `frontend.unused_files` / `unused_deps` |    0 | An orphaned module or an unused package is a mistake to fix, not a cost to absorb.                                                               |
| `file_size.max_python`                 |    60 | About 1.5% of the current worst file, and the size of one feature's worth of lines in an already-long module.                                     |
| `file_size.over_limit_python`          |     1 | One file crossing the 600-line review threshold.                                                                                                  |
| `file_size.max_frontend`               |    60 | Same as the Python band, against a 4042-line worst offender.                                                                                      |
| `file_size.over_limit_frontend`        |     1 | One file crossing the frontend threshold.                                                                                                         |
| `duplication.percent`                  |  0.05 | Smaller than the 0.03 a fourth device map cost (row above), scaled to leave room for one such file without covering a copy-pasted module.          |

Hard-gated and report-only metrics carry no slack at all, and a test fails if
one ever appears there: a hard rule with a band is not a rule, and a number
that never fails the gate cannot use headroom.

Slack does not replace burning debt down. It buys the PR in front of you room
to land, once, and `just quality-baseline` will not turn that room into a new
allowance. The moment the tree drifts up into a band, the next PR on that
axis is red again unless someone ratchets the allowance down first, which is
exactly the pressure this file is here to apply.

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
- the merge base cannot be measured (the ref is missing or the base tree will
  not run): `base_compare` reports UNKNOWN (exit 2), never REGRESSION, and the
  output says why. The gate never downgrades on a guess and never passes
  silently. HEAD is itself on main is different: that is a measured regression
  on the trunk detector, not a failed inheritance check, so it stays REGRESSION.

The base re-measure only runs when something actually regressed, and only for
the evaluator that owns the regressed metric. It checks the base out with
`git worktree add --detach` into a throwaway tempdir and runs that commit's
own committed copy of the gate there, so a metric over `scripts/` is measured
against the base's version of the file and a branch-only flag does not have to
exist on main for the measurement to work. Hard-zero rules are never offered
the downgrade: a broken architecture contract has no allowance to be "over"
on main.

## Main push/schedule: absolute counts are a trend report, not a gate

On a push or schedule CI run, HEAD is main's own tip, so there is no merge-base
to inherit from (issue #3246). Those runs pass `--main-report-only` to
`scripts/quality_gate.py`; pull_request runs do not.

A plain-ratchet metric above its `baseline.json` allowance still prints
`REGRESSION` and is written to the GitHub Actions job summary as a non-blocking
trend report, but the step exits 0. The enforced gate remains the per-PR delta
vs merge-base described above: a PR that makes an already-over metric worse
still fails, and a PR that adds nothing to trunk debt still passes via
`INHERITED`.

`HARD_ZERO` gates (`sync_drift`, shell constructs, architecture contracts) are
unchanged everywhere, including on main: they still fail hard. See
[ADR-NEW-quality-ratchet-new-code-gate.md](../../docs/decisions/ADR-NEW-quality-ratchet-new-code-gate.md)
and issue #3246.

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

### Sat 5 Sep 2026: waveform track artwork crosses the api-rb.ts ceiling

| metric                | was | now | what is in the gap                                                                                                                                                                                          |
| --------------------- | --- | --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `frontend.max_fan_in` | 36  | 38  | Top module is now `src/lib/rb/api-rb.ts` (it overtook `client.ts` since the allowance was measured); #1297's `WaveTrackSummary.svelte` is its 38th importer, taking `artworkUrl`. Recorded to clear a red trunk. |

Owner and payback: `baseline.json` entry `WAVE_ARTWORK_API_RB_FAN_IN`. The
same split that #677 names for `client.ts` applies to `api-rb.ts`.

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

## Scored rubric (not this ratchet)

The per-surface code-quality rubric in [`rubric/README.md`](rubric/README.md)
scores `open-dj`, `apps/open_dj`, `apps/adapters`, and
`apps/webui/frontend` independently. It catches doc and contract defects that
lint and tests miss. It does **not** update `baseline.json` and is not part of
`just quality`.
