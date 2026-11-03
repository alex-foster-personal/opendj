# Bundle budget history before charges

Write-once record. These derivations sat in `scripts/bundle-budget.mjs` above the `BUDGETS`
array until Tue 3 Nov 2026, when the limits moved to `budgets/<surface>/base.json` plus per-PR
charges (ADR-NEW-bundle-budget-charges). Each surface's own dated raises are in
`budgets/<surface>/HISTORY.md`. Moved verbatim, comment markers removed.

```text
Each limit is a real measurement, not a round number picked for comfort.
Baseline build: c3cf9329 (main), Mon 31 Aug 2026, `pnpm build`.

library      256,000 bytes. UNCHANGED from the original gate. This is a bug
             fix to what gets counted, not a raise. Measured 93,011 (36.3%).
             The generous remaining headroom is inherited, not newly granted.
performance  measured 193,544, +5% headroom, rounded up to the next KiB
             => 203,776 (199 KiB). A ratchet at today's number: the route
             carries the app's real weight and may not silently grow. The
             measurement includes the xrun sentinel AudioWorklet (1,636),
             which the predecessor gate never counted at all.
             RAISED Sat 5 Sep 2026 (PR #1288, pin 93c82bb36eb7): the output
             liveness health bar under master volume adds a real, permanent
             part of the always-rendered performance UI (audio-output-health
             .svelte.ts, audio-output-health-display.ts, the liveness
             onSnapshot hook) - genuine new weight, not padding. Clean
             origin/main (54a6220cc) already measured 204,275 of 204,800
             (525 bytes headroom) before this PR landed, so this was going
             to trip on the next few bytes of ordinary feature work
             regardless. With this PR: measured 205,141, +5% ceil-to-KiB
             => 216,064 (211 KiB).
             RAISED Thu 10 Sep 2026 (PR #1010, pin 593b23ffe): the
             discussion_r3975326238 fix threads a new isSuperseded() guard
             through refreshAnalysisSourceDecks/_refreshDecks/_adopt in
             analysis-source.svelte.ts and analysis-source-refresh.ts,
             plus the JSDoc explaining the race it closes. CI measured
             216,952 on this head (deterministic across two independent
             CI builds); the equivalent local `pnpm build` on the exact
             same pinned toolchain (Node 22.14.0, pnpm 11.9.0, svelte
             5.56.7, @sveltejs/kit 2.70.1, vite 5.4.21, terser 5.51.2 -
             all confirmed byte-for-byte against CI's own install log)
             measured only 214,470 for the identical source tree - a
             pre-existing local-vs-CI gap (214,450 vs 214,747, ~300
             bytes) that grew to ~2,500 bytes specifically on this diff.
             Isolated with `pnpm build` chunk tables from both sides: the
             entire delta sits in the one large shared chunk carrying
             this route's bulk (HUoDj3ab.js on CI / I_4JPHqH.js locally),
             which is exactly the chunk these edited files land in -
             consistent with terser's frequency-based mangling producing
             a slightly different (non-hermetic) minification result
             when Rollup's module concatenation order shifts with
             filesystem enumeration order between the two build
             environments, not with new dead weight. Ratcheting on CI's
             own number since that is what the gate actually runs
             against: 216,952, +5% ceil-to-KiB => 228,352 (223 KiB).
             ALSO RAISED Thu 10 Sep 2026 (PR #1587, merged into main
             independently): CI (`frontend unit + check + build`, run
             34458533006, job 102810569554) measured 216,124 on main's
             tip alone, 60 bytes over the prior 216,064 ceiling, from
             unrelated feature work plus the same local-vs-CI build gap
             documented above. That PR's own ratchet, 227,328 (222 KiB),
             is superseded here by this merge because PR #1010's ceiling
             (228,352) is the larger of the two and covers both diffs'
             combined weight; re-measure on the merged head before
             tightening either number back down.
             RAISED Fri 11 Sep 2026 (found while landing #1555, nav1-key-
             record - a backend-only lane whose sole frontend diff is a
             JSDoc comment in api-types.ts, which cannot change emitted
             JS): pristine origin/main at 7c68dd0f3 already measured
             229,639 of 228,352 (11 files, over by 1,287), confirmed with
             a clean `git worktree add --detach` off that exact SHA with
             no branch changes applied, so this was inherited trunk
             growth, not a regression from this PR. `git log --oneline
             e0213b060..7c68dd0f3 -- apps/webui/frontend/src` is
             non-empty: several unrelated merges landed frontend work in
             that range. Ratcheting on the pristine-trunk number per the
             +5% ceil-to-KiB rule: 229,639, +5% => 241,121, ceil to KiB
             => 241,664 (236 KiB).
other-lazy   measured 60,160, same +5% ceil-to-KiB rule => 63,488 (62 KiB).
             RAISED Mon 1 Sep 2026: the CloudSync config route (/cloudsync
             policy matrix, machines, pins, overview + api-cloudsync client)
             is a genuine 17th route in this shared bucket; new measured
             64,328, same +5% ceil-to-KiB rule => 67,584 (66 KiB). Widened
             deliberately per the rule above, not by accident.
             RAISED again Fri 11 Sep 2026 and Wed 16 Sep 2026; the derivation
             for each lives beside the BUDGETS entry rather than here, so the
             running figure is stated in one place only.
             This surface exists so that no chunk is unmeasured. It spans every
             route that is not "/" or /performance, plus the error template, so
             its headroom is TIGHT by construction and it absorbs feature work
             from the whole app; widen it deliberately if ordinary feature work
             starts tripping it, but widen it visibly rather than by accident.

The +5% rule applies to the two ratcheted budgets (performance, other-lazy)
so neither carries slack invented case by case; library keeps its inherited
ceiling as documented above. Raising any number below is a deliberate,
reviewable act.
```
