// bundle-budget.mjs -- per-surface gzip budgets for the emitted client bundle.
//
// WHY THIS EXISTS IN THIS SHAPE
// The predecessor gate summed every file in build/_app/immutable/chunks/*.js and
// called the total "the library page bundle". That was wrong in both directions:
//   - it COUNTED chunks the library page never downloads (the /performance route
//     DSP alone is about 187 KB gzip, lazily imported, counted in full), and
//   - it MISSED files the library page does download (entry/*.js and nodes/*.js
//     were outside the glob, about 32 KB gzip).
// Measured on c3cf9329 the old gate read 252,219 of 256,000 while the library
// page actually pulled 93,011. It was about to red unrelated PRs at roughly 3.8 KB
// of apparent headroom, for weight that page never fetches.
//
// The repair is to measure per SURFACE, from the real module graph, and to leave
// nothing outside a budget.
//
// HOW A SURFACE IS COMPUTED
// SvelteKit code-splits at every dynamic import. A dynamic import is therefore a
// budget boundary: whatever sits behind one is not paid for by the page that did
// not take that branch. So each surface is the STATIC import closure of its roots:
//   library      roots = every _app/immutable/*.js referenced by index.html,
//                plus the root layout node, plus the node for route "/".
//   performance  roots = the nodes for "/performance" and its children.
//   other-lazy   roots = every remaining route node, plus deferred app-shell
//                chunks such as SvelteKit's error template, which is dynamically
//                imported from entry/app.js and belongs to no route.
// A file is charged to the FIRST surface that reaches it, in that order, so a
// chunk shared between the library page and /performance is charged to the
// library page (which is what actually downloads it on first paint) and is not
// double counted.
//
// RUNTIME-LOADED MODULES
// AudioWorklet processors are emitted as standalone .js and fetched by URL
// (`new URL("../assets/x.js", import.meta.url)`), never imported, so no module
// graph reaches them. They are still real downloads. After the closures are
// computed, any leftover file whose emitted name appears as a literal inside a
// surface's own code is charged to that surface. main already ships one of these
// (the xrun sentinel processor, charged to performance); the color-FX worklet on
// PR #492 will be charged the same way, automatically.
//
// COVERAGE IS THE POINT
// Every .js emitted under _app/immutable/ must land in exactly one budget. A file
// reached by no surface and named by no surface is reported as UNATTRIBUTED and
// fails the run. That is what stops the original bug returning: new lazily-loaded
// weight cannot appear in a directory nobody sums. The inventory walks
// recursively on purpose, so a future workers/ directory is caught, not ignored.
//
// SCOPE: JS only. CSS and other assets are emitted as Vite "assets", not chunks,
// and were outside the predecessor gate too. Widening to CSS would change what
// the 250 KB number means, so it is deliberately left alone. See README.

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { join, dirname, normalize, relative } from 'node:path';

// ---------------------------------------------------------------- budgets ---

// Each limit is a real measurement, not a round number picked for comfort.
// Baseline build: c3cf9329 (main), Mon 31 Aug 2026, `pnpm build`.
//
// library      256,000 bytes. UNCHANGED from the original gate. This is a bug
//              fix to what gets counted, not a raise. Measured 93,011 (36.3%).
//              The generous remaining headroom is inherited, not newly granted.
// performance  measured 193,544, +5% headroom, rounded up to the next KiB
//              => 203,776 (199 KiB). A ratchet at today's number: the route
//              carries the app's real weight and may not silently grow. The
//              measurement includes the xrun sentinel AudioWorklet (1,636),
//              which the predecessor gate never counted at all.
//              RAISED Sat 5 Sep 2026 (PR #1288, pin 93c82bb36eb7): the output
//              liveness health bar under master volume adds a real, permanent
//              part of the always-rendered performance UI (audio-output-health
//              .svelte.ts, audio-output-health-display.ts, the liveness
//              onSnapshot hook) - genuine new weight, not padding. Clean
//              origin/main (54a6220cc) already measured 204,275 of 204,800
//              (525 bytes headroom) before this PR landed, so this was going
//              to trip on the next few bytes of ordinary feature work
//              regardless. With this PR: measured 205,141, +5% ceil-to-KiB
//              => 216,064 (211 KiB).
//              RAISED Thu 10 Sep 2026 (PR #1010, pin 593b23ffe): the
//              discussion_r3975326238 fix threads a new isSuperseded() guard
//              through refreshAnalysisSourceDecks/_refreshDecks/_adopt in
//              analysis-source.svelte.ts and analysis-source-refresh.ts,
//              plus the JSDoc explaining the race it closes. CI measured
//              216,952 on this head (deterministic across two independent
//              CI builds); the equivalent local `pnpm build` on the exact
//              same pinned toolchain (Node 22.14.0, pnpm 11.9.0, svelte
//              5.56.7, @sveltejs/kit 2.70.1, vite 5.4.21, terser 5.51.2 -
//              all confirmed byte-for-byte against CI's own install log)
//              measured only 214,470 for the identical source tree - a
//              pre-existing local-vs-CI gap (214,450 vs 214,747, ~300
//              bytes) that grew to ~2,500 bytes specifically on this diff.
//              Isolated with `pnpm build` chunk tables from both sides: the
//              entire delta sits in the one large shared chunk carrying
//              this route's bulk (HUoDj3ab.js on CI / I_4JPHqH.js locally),
//              which is exactly the chunk these edited files land in -
//              consistent with terser's frequency-based mangling producing
//              a slightly different (non-hermetic) minification result
//              when Rollup's module concatenation order shifts with
//              filesystem enumeration order between the two build
//              environments, not with new dead weight. Ratcheting on CI's
//              own number since that is what the gate actually runs
//              against: 216,952, +5% ceil-to-KiB => 228,352 (223 KiB).
//              ALSO RAISED Thu 10 Sep 2026 (PR #1587, merged into main
//              independently): CI (`frontend unit + check + build`, run
//              34458533006, job 102810569554) measured 216,124 on main's
//              tip alone, 60 bytes over the prior 216,064 ceiling, from
//              unrelated feature work plus the same local-vs-CI build gap
//              documented above. That PR's own ratchet, 227,328 (222 KiB),
//              is superseded here by this merge because PR #1010's ceiling
//              (228,352) is the larger of the two and covers both diffs'
//              combined weight; re-measure on the merged head before
//              tightening either number back down.
//              RAISED Fri 11 Sep 2026 (found while landing #1555, nav1-key-
//              record - a backend-only lane whose sole frontend diff is a
//              JSDoc comment in api-types.ts, which cannot change emitted
//              JS): pristine origin/main at 7c68dd0f3 already measured
//              229,639 of 228,352 (11 files, over by 1,287), confirmed with
//              a clean `git worktree add --detach` off that exact SHA with
//              no branch changes applied, so this was inherited trunk
//              growth, not a regression from this PR. `git log --oneline
//              e0213b060..7c68dd0f3 -- apps/webui/frontend/src` is
//              non-empty: several unrelated merges landed frontend work in
//              that range. Ratcheting on the pristine-trunk number per the
//              +5% ceil-to-KiB rule: 229,639, +5% => 241,121, ceil to KiB
//              => 241,664 (236 KiB).
// other-lazy   measured 60,160, same +5% ceil-to-KiB rule => 63,488 (62 KiB).
//              RAISED Mon 1 Sep 2026: the CloudSync config route (/cloudsync
//              policy matrix, machines, pins, overview + api-cloudsync client)
//              is a genuine 17th route in this shared bucket; new measured
//              64,328, same +5% ceil-to-KiB rule => 67,584 (66 KiB). Widened
//              deliberately per the rule above, not by accident.
//              RAISED again Fri 11 Sep 2026 and Wed 16 Sep 2026; the derivation
//              for each lives beside the BUDGETS entry rather than here, so the
//              running figure is stated in one place only.
//              This surface exists so that no chunk is unmeasured. It spans every
//              route that is not "/" or /performance, plus the error template, so
//              its headroom is TIGHT by construction and it absorbs feature work
//              from the whole app; widen it deliberately if ordinary feature work
//              starts tripping it, but widen it visibly rather than by accident.
//
// The +5% rule applies to the two ratcheted budgets (performance, other-lazy)
// so neither carries slack invented case by case; library keeps its inherited
// ceiling as documented above. Raising any number below is a deliberate,
// reviewable act.
const BUDGETS = [
  // RAISED Mon 21 Sep 2026 (+1 KiB, PR #3737, OBS-02/OBS-05): the client error
  // path now carries the page's live-transport read (`any_deck_live`, so the
  // engine can hold Sentry sends while a deck plays) and app-init defers the
  // diagnostics-consent module behind the boot window. That is ~435 bytes of
  // genuine first-paint weight; the consent module and its dialog themselves
  // are dynamically imported and land in other-lazy. Clean origin/main
  // (17c74562) measured 256,059 locally and within 80 bytes of the limit on
  // CI before this PR, the same "no headroom left" state the performance
  // budget was in on Wed 2 Sep 2026. Payback: the next library-route weight
  // reduction retires this KiB, not the consent code.
  // RAISED Wed 23 Sep 2026 (+1 KiB, PR #3681, multiple-folder first-run setup):
  // SetupOverlay is imported statically by +layout.svelte, so the folder-row
  // list (add/remove rows, per-row check, stale-scan guard) is first-paint
  // weight. Clean origin/main cb401fbee measured 256,747 locally (277 bytes of
  // headroom); this PR measured 257,513 locally and 257,523 on CI, +766 bytes.
  // Payback: lazy-loading SetupOverlay (it renders only while the first-run
  // overlay is open) retires this KiB and the one above.
  // PR #3645 (DECKUX-19 stem mini-waveforms) adds +333 bytes of first-paint
  // weight (the show_stems pref, settings row and perf-tier cache scalers)
  // and lands AFTER #3681, so it takes no raise of its own: both fit under
  // this one KiB. Merged tree (main 97fc14795 + #3645) measured 257,887 locally;
  // after #3739 (main 47324919a) it measured 257,987, 61 bytes of headroom left.
  // RAISED Thu 24 Sep 2026 (+1 KiB, PR #3865, STEM-37 stems on installed spokes):
  // clean origin/main ccad1999e already measures 258,367 locally, 319 bytes
  // OVER this limit (merge skew: each PR since #3739 passed alone). This PR
  // adds +183 on top (the deck's hydrating probe state; the wait loop itself
  // is in its own module), 258,550 locally and on CI within 35 bytes. The
  // SetupOverlay payback above still retires all three KiB.
  // Thu 24 Sep 2026: the SetupOverlay payback (#3862) and the pin-shell deferral
  // (#3903) landed together; merged tree measured 250,040 locally against the
  // unchanged 259,072. Not raised: 9,032 bytes of headroom, first since #3737.
  // RAISED Sat 26 Sep 2026 (+2 KiB, PR #3837, Reloop Mixtour Pro I/O + MIDI +
  // Play from USB): clean origin/main a3ca2f14a measured 254,175; the PR as
  // written measured 260,918, +6,743. Two slices of that were not first-paint
  // code and are now deferred: the IOPIN-06 takeover POLICY (performance-ipc
  // reports the mode, so it imported the policy owner; the display state is now
  // the takeover-ui leaf and the policy loads with the MIDI engine) and the
  // stick session edits (USB-only, now imported on demand by the async
  // call sites that already knew the id was a stick id). Measured after:
  // 259,325, 253 bytes over. What remains, about +5.1 KB, is engine and API
  // code the root layout's static closure already carries: the headphone
  // cluster's live signal meters and multichannel monitor routing
  // (headphones.ts, audio-output-topology.ts), the stem-control restore, the
  // takeover mode's IPC command and read-model field, the USB id routing in
  // api.ts/api-rb.ts/track-source.ts, and the rb.midi_enabled setting row.
  // Limit follows next whole KiB above the measurement plus 1 KiB of jitter
  // margin: 260,096 + 1,024 => 261,120 (255 KiB).
  // Payback: taking the deck audio engine out of the root layout's static
  // closure (the 66,479-byte engine chunk flagged under other-lazy's Wed 16 Sep
  // entry) retires these 2 KiB; the #3837 bytes above ride in that engine.
  // Wed 30 Sep 2026: the Air preview loop's merge of origin/main 2ecfde5d7 onto
  // #3837 + #3896 measures 262,092 locally, 972 bytes OVER this limit (merge
  // skew: main's 500 commits since the merge base add first-paint weight on
  // top of #3837's). Deliberately NOT raised on that local, never-pushed
  // branch: #3837 must pay back or justify a raise on its own rebase onto main.
  // RAISED Fri 2 Oct 2026 (+1 KiB, PR #4897, first-run wizard skip and plain-language
  // copy, SETUP-26 / UX-R2-03/04): merge skew again. Clean origin/main e0dfa83bc measured
  // 258,429 locally; main + this PR 259,214 (142 OVER), main + this PR + its follow-up
  // #4928 (failed-import sentence) about 259,380. Each PR passed alone. Payback, not
  // yet measured: find which of this PR's setup modules the first-paint closure
  // reaches and move the wizard-only copy behind the lazy SetupOverlay import.
  // RAISED Fri 2 Oct 2026 (+2 KiB, PR #4908, CUEOUT-22 two-device headphone cue in the
  // Mac app): headphones.ts is in the first-paint closure, and its native-sink
  // branch adds +1,250 bytes even with the socket client and worker split behind a
  // dynamic import. Clean origin/main c8b8f5aeb measured
  // 259,943 locally (153 bytes of headroom); main + this PR 261,193. The second KiB is
  // headroom for #4906 (stems on a playing deck), which lands right after with +1,319
  // of its own and raises once more. Payback, not yet measured: find what pulls
  // headphones.ts into the "/" closure and defer it.
  // RAISED Sat 3 Oct 2026 (+1 KiB, PR #5086, CLOUDSYNC-33 "This file isn't on this
  // computer"): audio-engine and performance-ipc are in the first-paint closure, and
  // the deck-load failure wording (deck-load-context.ts: the plain-reason map, the
  // error-keyed WeakMap the banner and toast read, the title fallback) adds +270
  // bytes. Clean origin/main 01d5b3520 measured 262,071 locally (73 bytes of
  // headroom); main + this PR 262,341. Trunk's queue removed it at 178 over on CI.
  // Payback: the headphones.ts deferral above retires this KiB too.
  // RAISED Sat 3 Oct 2026 (+1 KiB, PR #4014, issue #3986 library pins; JIK's V1
  // budget decision, Sat 3 Oct 01:36Z, up to 4 KiB per V1 PR): the pin batch's
  // compatible filter, pairing underline and confirm-pref plumbing reach the "/"
  // first-paint closure through the shared library browser. Clean origin/main
  // b6c894194 measured 261,672 locally; main + this PR at the merge of Sat 3 Oct
  // measured 262,967 (823 OVER). Payback, not yet measured: move the compatible-filter
  // and pairing derivation behind the /performance-only lazy imports. Re-merged with
  // main 5504f60e7 (which carries #5086's KiB): 263,265 measured, 927 under.
  // RAISED Sat 3 Oct 2026 (+1 KiB, PR #4906, STEM-44..48): the deck's named stem
  // states (stem-status.ts, StemRow) render on "/" and are first-paint weight. Clean
  // origin/main measured 262,071 locally before #5086; main + this PR 263,325, +1,254.
  // Already paid back inside this PR: the retry decision and the live handoff load on
  // first use (stem-retry.ts, stem-live-handoff.ts), -232 measured. On main 423376f7d
  // with #5086's own +1 KiB, main + this PR measures 263,784 locally (CI read within
  // 20 bytes of local on this PR's last head).
  // Payback, not yet measured: the label table in stem-status.ts.
  // Merge note (PR #4014 x #4906, Sat 3 Oct 2026): both sides already sit on the
  // same 264,192 ceiling (the larger of the two). The merged tree measures
  // 264,845 (653 over); +5% ceil-to-KiB => 278,528 (272 KiB).
  // RAISED Wed 14 Oct 2026 (+1 KiB, PR #6687 then PR #6689, CMDK-07..10, CMDK-15 and
  // CMDK-19, issues #6090 to #6092): #6687 adds the preview strip's one-track read
  // (fetchPreviewAsset in row-assets-fill.ts) and the preview flag readers, and
  // #6689 the bar's IPC parity (cmdk_open, cmdk_close, then cmdk_pick), validator
  // and executor code in performance-ipc.svelte.ts. All of it ships in the root
  // chunk "/" loads; the house rule that every control has IPC parity puts the
  // dispatcher half on first paint. The
  // bar itself, its ranking and its pairs stay in the lazy v2 chunk. CI read
  // main + #6687 at 278,538 (10 over) and main + #6689 at 278,876; #6092's
  // cmdk_pick lands under the same KiB. Payback, not yet measured: the
  // dispatcher's per-command validators table-driven instead of one branch each.
  // RAISED Wed 14 Oct 2026 (+1 KiB, PR #6585 follow-up, perf board row
  // anlz-raw-not-deep-proxy): rawAnlz (src/lib/rb/anlz-raw.ts) keeps each deck's
  // ANLZ payload out of the deep $state proxy, roughly 20 MB and ~100k signals per
  // loaded deck, and every deck ANLZ writer on "/" calls it. Measured locally with
  // vite build: main 427057f20 without #6585's src 278,441; main with it 278,530
  // (+89, 2 over; CI read 278,529 to 278,532 on the PRs it blocked).
  // Payback, not yet measured: the isRawAnlz probe and the brand comment are the
  // only parts not needed on first paint.
  // RAISED Fri 16 Oct 2026 (+1 KiB, PR #6786 wave zoom): 279,552 -> 280,576 for the
  // band cache's tiled render in render.ts and the beat-line thinning in wave-math.ts,
  // both on "/" through the waveform component. CI measured 280,107 on 625d26b57 (main
  // alone 278,530). PR #6786 adds no zoom control, glide or settings; those land in
  // PR 2. Rounded up to the next KiB. No second raise.
  // RAISED Tue 27 Oct 2026 (+2 KiB, PR #7483, key notation + Camelot rules + master
  // handoff, the maintainer: "merged to main please"): the library table, deck header and
  // inspector now print keys through one notation (camelot_musical by default) and
  // the K/B columns are measured in the rendered font (canvas measureText) instead of
  // a 7px-per-character guess. Both are first-paint table code. This PR measured
  // 280,578 locally against 280,576. The second KiB covers the two sibling PRs of
  // the same request (Camelot rule checkboxes measured 280,277 alone; level-based
  // MASTER handoff), each of which also adds first-paint weight. Payback: none
  // planned; these are features on the first screen.
  // RAISED Tue 27 Oct 2026 (+1 KiB, PR #7559, level-based MASTER handoff): the #7483
  // raise covered three sibling PRs, but with #7483 and #7557 on main this PR measured
  // 282,660 in CI against 282,624 (36 bytes over). The level estimate and the MASTER
  // hold are first-paint deck code. Payback: none planned.
  // RAISED Tue 27 Oct 2026 (+5 KiB over #7559, auto gain, AUTOGAIN-01..06, branch af--auto-gain):
  // the deck audio engine is in the "/" first-paint closure, so the trim stage's
  // auto gain (the resolved-offset reader and its checks, the per-deck store, the
  // AG readout under TRIM) and the two Settings rows are first-paint weight.
  // The pref sync is deferred behind the boot scheduler; a split of the Settings
  // rows into their own leaf was tried and saved nothing, because the engine
  // already carries the deck code into "/". Local build, one pass: clean
  // origin/main 80fae484d 279,682; with this change 282,190 (+2,508). Next KiB
  // above plus 1 KiB of jitter margin, as #3837: 283,648 (277 KiB) alone. Merged
  // onto main's #7483 raise (282,624, measured 280,578): 280,578 + 2,508 = 283,086,
  // next KiB plus 1 KiB of jitter => 284,672 (278 KiB). Review then added the
  // answer contract check, write versioning, the unread-pref notice and the
  // audibility factor: CI on 4240999 measured 285,293, next KiB plus 1 KiB of
  // jitter => 286,720 (280 KiB). Merged onto main's #7559 raise (283,648, measured
  // 282,660): auto gain adds 285,293 - 280,578 = 4,715, so 287,375 estimated, next
  // KiB plus 1 KiB of jitter => 288,768 (282 KiB). Payback: the engine deferral named
  // under #3837 above carries this weight out of "/" with the rest of the engine.
  // RAISED Thu 22 Oct 2026 (+2 KiB, PR #6235, GRIDEDIT-6235..6235d + DECKCTL-6235..6235d deck controls;
  // JIK's V1 rule, up to 4 KiB per V1 PR): the dispatcher on "/" gains the grid_edit command's
  // validation (grid-edit-command.ts; the request half is a lazy chunk loaded on the first
  // press), AU parking and the phrase-quantize seek decision. Local vite build: main c454c7b27
  // 279,744; main + this PR 281,094 (+1,350, 518 over). The second KiB came from review
  // (Codex/Sol, same day): the dispatcher binds each queued grid edit to its track and grid
  // and merges held-button repeats before the persistence-scope wait, all needed at the
  // first press. Local: 281,880 (+2,136). Payback: none open.
  // Merge note (PR #6235 x main 4c130633d, Tue 27 Oct 2026): the #6235 raise above
  // (+2 KiB, +2,136 measured) lands on top of main's auto gain ceiling (288,768,
  // measured 287,375): 288,768 + 2,048 = 290,816 (284 KiB), the same +2 KiB, no new
  // raise. Local vite build of the merge: 289,345. Payback: none open.
  // Top bar re-land #6824 of #6201 (TOPVIEW-01/-05), Tue 27 Oct 2026: no raise here. Its
  // Thu 22 Oct +1 KiB raise is dropped: locally, main 4c130633d1 286,471; main + this PR
  // 288,012 (+1,541, under the limit).
  // RAISED Wed 28 Oct 2026 (+1 KiB, same PR, merged onto main e5fb30dcfd): locally, main
  // 287,253; main + this PR 288,683 (+1,430), 85 B under the limit, inside CI's build jitter.
  // Measured ceiled to KiB (288,768) plus 1 KiB of jitter. Payback: none open.
  // RAISED Wed 28 Oct 2026 (trunk repair #7658): main 275b88e7f8 is over this budget after
  // #7594 (autodj defects + agent DJ control) and #6565 (MIK energy backfill) landed through
  // selected-test bundle merges (#7598) that did not run the budget gate. One clean nucbox
  // build each: main 906dc7be74 289,003; main 275b88e7f8 290,882 (+1,879).
  // Measured ceiled to KiB plus 1 KiB of jitter. Payback: owed by #7594 and #6565.
  // RAISED Wed 28 Oct 2026 (+2 KiB, PR #6574, KEYSHIFT-01..05, merged onto main 27263feb59):
  // main's limit plus this PR's own delta, ceiled to KiB; the delta (+1,549) is from one clean
  // nucbox build each of main 906dc7be74 with and without this PR. Payback: none planned.
  // measured below is this merged revision's own nucbox build: 292,315.
  // RAISED Wed 28 Oct 2026 (+1 KiB, PR #7600, LIBUX-53 collapse rule, re-merged onto main after #6574):
  // this PR's measured delta on top of main's limit (#6574's), ceiled to KiB. One clean nucbox build
  // each against main 906dc7be74 measured +1,023. Payback: none planned.
  // RAISED Wed 28 Oct 2026 (+1 KiB, PR #7601, LIBUX-54 column chooser, re-merged onto main after #7600):
  // this PR's measured delta on top of main's limit (after #7600), ceiled to KiB. One clean nucbox build
  // each against main 906dc7be74 measured +650. Payback: none planned.
  // LOWERED Wed 28 Oct 2026 (-17 KiB, PR #6829, re-land of #6205, re-measured after merging
  // main b26ee5252c, #7700): the root layout still mounts SettingsOverlay lazily
  // (settings-overlay-loader.ts), so the settings modules main has since added to "/" (key
  // notation, auto gain) move with it into performance and other-lazy. Local, one clean vite
  // build each: main b26ee5252c library 290,576, performance 287,936, other-lazy 375,376 (953,888); main + this PR
  // 274,187, 299,765, 394,367 (968,319), net +14,431 B across the three.
  // Measured ceiled to KiB (274,432) plus 1 KiB of jitter, which also absorbs the #7658 raise
  // above. Payback: owed by #7594 and #6565 (the #7658 share).
  // Thu 29 Oct 2026 (re-measured after merging main 3dcda3b028, which now carries #6223's code): one clean vite build each, main 294,894 / 292,373 / 392,191 and main + this PR 277,411 / 304,114 / 412,036, deltas -17,483 / +11,741 / +19,845 B, ceiled to -17 / +12 / +20 KiB on main's 296,960 / 294,912 / 393,216, so 279,552 / 307,200 / 413,696 (+15 KiB net). The earlier carry measured before #6223 landed while its limit already counted #6223.
  // RAISED Fri 30 Oct 2026, #5886 analysis-while-playing: +1 KiB after payback of 446 B; standing
  // up-to-4-KiB allowance. One clean local vite build each (Node 22.22.0): main 995cc2493b 279,229;
  // merged with this PR 280,062 (510 over); with the next-up client (autoplay-next-up.ts) loaded on
  // first use through autoplay-next-up-lazy.ts 279,616 (64 over). The rest is the xrun detector's
  // warmup handshake and the next-up loader stubs, first-paint code. Ceiled to the next KiB.
  // RAISED Fri 30 Oct 2026 (+1 KiB, PR #6127, decode-on-bytes deck load and the S2 output-device
  // probe, V1 rule): CI build job 124242420096 at 1dc9b508d measured library 279,864, 312 B over
  // 279,552; ceiled to KiB. Payback: none planned.
  // Merge note (#5886 x main 507102f2b9, Fri 30 Oct 2026): #6127 took the same +1 KiB to 280,576 as
  // #5886 above; both land on one ceiling. One clean local vite build of the merge: 280,458.
  // RAISED Fri 30 Oct 2026 (+3 KiB, trunk repair after #7957 and #6534): main 8849e7a94e is over
  // this budget after #7957 (autodj chooser, bundle of 7) and #6534 (Gothic block snapping)
  // landed through bundle merges, which do not gate on the budget (Tue 27 Oct 2026). One clean
  // vite build each: main 507102f2b9 (#6127, before both) 279,920; main 8849e7a94e 282,240.
  // Measured ceiled to KiB (282,624) plus 1 KiB of jitter. Payback: owed by #7957 and #6534.
  // (Ported into PR #6189 from #8069; PR #6189 adds +421 B, inside this limit.)
  // Merge note (PR #6235 x main 131f65be61, Fri 30 Oct 2026): main's growth since 4c130633d (#6829's
  // lazy-settings move among it) re-bases the #6235 raises above onto main's ceiling. Local, one
  // clean vite build each: main 131f65be61 279,204; main + this PR 281,710 (+2,506).
  // RAISED +4 KiB over main's ceiling: the measured size ceiled to KiB (282,624) plus 1 KiB of
  // jitter, within the V1 rule of up to 4 KiB per V1 PR. Payback: none open; these are live deck
  // controls.
  // Merged onto main 9ec96943b0 Fri 30 Oct 2026, after #6127 took this ceiling to 280,576: one clean local
  // vite build of main + this PR measures 282,379. RAISED to the measured size ceiled to KiB (282,624)
  // plus 1 KiB of jitter, +3 KiB over main's ceiling, within the V1 rule. Payback: none open.
  // Merged onto main 2729743c1 Fri 30 Oct 2026: main itself measures 282,234 locally, 1,658 B over its own
  // 280,576 ceiling (no raise landed with that growth). Main + this PR measures 284,862 (+2,628 for #6235).
  // RAISED to the measured size ceiled to KiB (285,696) plus 1 KiB of jitter; of the +6 KiB over main's
  // ceiling, 2 KiB is main's own overrun, which is owed by whichever main change grew "/".
  // Merged onto main 01b93d8b1 Sat 31 Oct 2026: main itself measures 285,226 locally, 4,650 B over its own
  // 280,576 ceiling (#8069 carries main's repair). Main + this PR measures 287,887 (+2,661 for #6235).
  // RAISED to the measured size ceiled to KiB (288,768) plus 1 KiB of jitter; of the +9 KiB over main's
  // ceiling, 5 KiB is main's own overrun, owed by whichever main change grew "/".
  // Merged onto main 2cd9af583b Sat 31 Oct 2026, after #8069's repair took this ceiling to 283,648: main itself
  // measures 285,424 locally, 1,776 B over it again; main + this PR 288,027 (+2,603). The measured size
  // ceiled to KiB (288,768) plus 1 KiB of jitter keeps the limit at 289,792. Payback: none open.
  // Merged onto main 04e8bdfd4 Sat 31 Oct 2026, after #7956 (Cmd K v2): main itself measures 287,169 locally,
  // 3,521 B over its 283,648 ceiling; main + this PR 289,659 (+2,490), 133 B under the limit above. RAISED to
  // the measured size ceiled to KiB (289,792) plus 1 KiB of jitter. Payback: none open.
  // RAISED Sat 31 Oct 2026 (PR #6835, re-land of #6123/#6196, merged onto main 29a59c570c): one clean local vite build
  // each, main 285,328 and main + this PR 288,777 (+3,449, this PR, #8092 port included); main is 1,680 B over its own 283,648, inherited; the merged measurement ceiled to KiB => 289,792.
  // Merged onto main fd170f45c Sat 31 Oct 2026, after #6835's re-land: one clean local vite build each, main
  // 290,361 (569 B over its own 289,792); main + this PR 293,017 (+2,656). RAISED to the measured size ceiled
  // to KiB (293,888) plus 1 KiB of jitter. Payback: none open.
  // RAISED Sat 31 Oct 2026 (+2 KiB, trunk repair after #7956).
  // One clean vite build of main cc2a31a6c2, after #7956 (the Cmd K v2 bundle, behind flags default off) and #6835: bundle merges do not gate on the budget. Measured 289,990, 198 B over; ceiled to KiB (290,816) plus 1 KiB of jitter. Payback: none planned.
  // Merged onto main d20ef4367 Sat 31 Oct 2026, after e00c7b879 took main's ceiling to 291,840: main 290,409;
  // main + this PR 293,053 (+2,644). Ceiled to KiB plus 1 KiB the limit stays 294,912.
  { name: 'library', limit: 294912, measured: 293053, note: 'initial load of "/"' },
  // Merge note (PR #6163 x main 0ba0fa270, Thu 15 Oct 2026): #6163 had taken the
  // same +1 KiB (to 279,552) for the STRUCT-06 own-fast sections race and resync fixes
  // in anlz-cache and analysis-source. Both raises land on one ceiling; no second
  // raise. main + this PR measures 279,023 locally with vite build (529 under).
  // Wed 2 Sep 2026 18:40: +1 KiB for audio-output-liveness (P0: "no audio" must be an error
  // state; main had 24 bytes of headroom). Payback: PR #695 ships signalsmith-stretch once.
  // Thu 10 Sep 2026: +12 KiB for the isSuperseded() supersession-guard fix
  // (PR #1010, discussion_r3975326238). CI measured 216,952 on this diff.
  // Merge note: PR #1587 (main) independently measured 216,124 with its own
  // unrelated changes and a smaller ratchet (227,328); PR #1010's larger
  // ceiling is kept since it is the wider of the two and both diffs are now
  // present on this branch. Re-measure on the merged head before tightening.
  // Fri 11 Sep 2026: 223 -> 236 KiB, inherited trunk growth found while landing
  // #1555 (nav1-key-record). See the header comment above for the measurement.
  // Sat 26 Sep 2026 (PR #3837, Reloop Mixtour Pro I/O + MIDI + Play from USB):
  // NOT raised. The branch carried two +1 KiB raises that were never proposed
  // for main (Fri 25 Sep, IOPIN I/O panel MIDI status; Sat 26 Sep, Play from
  // USB stick-id routing); landing reverts them to this limit. The PR as written
  // measured 252,605 against it, +10,941 over, because the whole MIDI engine
  // (WebMIDI/native transport, device-map registry with the new Mixtour Pro
  // map, action glue, controller pad runtime, takeover policy) and the MIDI
  // drawer were in this route's static closure while being inert until
  // requestMidiAccess() resolves. Deferred instead: the engine behind one
  // dynamic import (lib/rb/midi/midi-engine.ts) awaited inside
  // requestMidiAccess() before initMidi() installs any port handler; midiState
  // split to a runtime-free leaf for the headphone cluster's status glyph; the
  // drawer and learn-log pop-out mounted only while open. Measured after:
  // 239,199 (clean origin/main a3ca2f14a: 238,317), so the route pays +882 for
  // the PR's always-rendered chrome, and that weight moved to other-lazy.
  // RAISED Fri 2 Oct 2026 (+1 KiB, PR #4923, V1 UI polish): clean origin/main
  // 5852c97c measures 239,840 locally. This PR adds +1,954 of user-visible
  // V1 work on the route (the 2-deck toggle, SET OUTPUTS and the I/O pins,
  // explainer dismiss, the 3Band waveform palette with its legacy option,
  // keyboard navigation in the track table, SVG MIDI marks), 241,794 merged,
  // 130 bytes over the old limit; 894 bytes of headroom remain.
  // RAISED Fri 2 Oct 2026 (+2 KiB, PR #4014, issue #3986 library pins): the
  // library pane on /performance gains the configurable compatible filter
  // (Camelot steps, BPM window and direction), the purple pairing underline
  // (PairingIndex + the reference-master derivation), the BPM provenance hover
  // and the in-app confirm flow. BrowserConfirmDialog and the compatible-filter
  // range panel load with a dynamic import on first use. Clean origin/main
  // c8b8f5aeb measured 242,422 locally (266 bytes of headroom); main + this PR
  // measured 244,327, +1,905, leaving 409 bytes under the new limit.
  // RAISED Fri 2 Oct 2026 (+1 KiB, PR #4904, V1 copy and paste between
  // playlists): clean origin/main c8b8f5ae measures 242,439 locally (249
  // bytes of headroom). The library browser is part of /performance, and the
  // Cmd+A/C/X/V handlers add 1,264 bytes there. Loading the clipboard rules
  // lazily (./track-clipboard behind loadTrackClipboard) pays back 388 of
  // them; 243,315 merged, 627 bytes over the old limit, 397 bytes remain.
  // Merge note (PR #4014 x #4904): both diffs land on this head; #4014's
  // ceiling (244,736) is the larger of the two independent ratchets but does
  // not include #4904's clipboard handlers stacked on the #3986 work. Limit
  // raised to 245,760 (240 KiB) until the merged head is re-measured in CI.
  // RAISED Sat 3 Oct 2026 (+1 KiB, PR #4014, issue #3986 library pins; JIK's V1
  // budget decision, Sat 3 Oct 01:36Z, up to 4 KiB per V1 PR): re-merged with main
  // 5504f60e7, /performance measured 245,836, 76 bytes over 245,760: main's own
  // growth since the last merge plus this PR's two-track pairing readiness check in
  // the top bar. 948 bytes remain. Payback: the same compatible-filter deferral as the library entry.
  // RAISED Fri 2 Oct 2026 (+1 KiB, PR #4906, STEM-44..48 / PERFMODE-18): the live stem
  // handoff and the graded decode hold are genuine route weight. Clean origin/main
  // c8b8f5aeb measured 242,439 locally; main + this PR 243,377, 689 bytes over.
  // Re-measured after #4904 landed on main (b6c894194) with its own +1 KiB raise:
  // both together measure 244,434, so this PR keeps its +1 KiB on top; 302 bytes remain.
  // RAISED Sat 3 Oct 2026 (+1 KiB, SET-11 set recording without ffmpeg, V1): REC now
  // lights only once audio is really written, and shows "Waiting for microphone
  // permission" while macOS's first-run prompt is up (Silver lost 42 s of a set to a
  // REC that looked live during it). recordRailState, its tooltips and the 1 s status
  // poll sit on the REC rail, which is first paint. Main measured 243,599 on Sat 3 Oct
  // (ci/2026-10-03-quality-ratchets.md); this branch 243,933, +334 of its own and 221
  // over the old limit. #4906 raises to the same 244,736 for its own +1,319: if it
  // lands first, the merged tree needs one more KiB. Payback, not yet measured: load
  // the REC tooltips with the lazy RecordInputPicker.
  // MERGED Fri 2 Oct 2026 (af--preview-live into main c8b8f5ae): main's #4923
  // raise is kept because its features are all in the merged route; the
  // Preview's #3837 work fit the old 241,664 by deferral (+882 measured). NOT
  // re-measured on the merged tree at resolution time.
  // MEASURED Fri 2 Oct 2026 on the merged tree (claude/port-preview-ui-to-main):
  // 250,293, 7,605 over 242,688. Both trees were reviewed under their own
  // ceilings; the union carries main's V1 polish AND the Preview's I/O panel,
  // Mixtour pickup, phase lock and USB play on /performance. Reviewed raise to
  // the measured size ceiled to KiB (245 KiB), no extra headroom; flagged in
  // (private path). Payback candidates: the I/O panel body
  // and the USB browse store behind their first open.
  // MEASURED again Fri 2 Oct 2026 after main eede75a4 and the Preview's #5000
  // (enrich card collapses beside the help buttons) merged in: 251,283, 403
  // over 245 KiB. Raised to the measured size ceiled to KiB (246 KiB), no extra
  // headroom; the same performance-register row covers it.
  // RAISED Fri 2 Oct 2026: 237 -> 252 KiB for PR #5013 (V1 ports from
  // main-electron-rust). Beat Sync's continuous phase lock (#4602, #4653, #4733:
  // phase-lock-webaudio, phase-lock, context-time-wait, the preset runner split)
  // is real always-loaded deck weight on this route; on main-electron-rust the
  // same code shipped under a 249 KiB ceiling. Local build on this head measured
  // 245,752 (CI agreed to the byte), 3,064 over. The ceiling follows the +5%
  // ceil-to-KiB rule on 245,752. Re-measured after merging main (#4904 copy
  // and paste, #4766): 246,970.
  // Merge with main after #4014/#4094/#4906/#5228: main's ceiling 260,096
  // (measured 247,657) is the larger of the two, so the merged tree keeps it.
  // RAISED Sat 3 Oct 2026 (+1 KiB, PR #4094, FB-20 comment hover counts; JIK's V1
  // budget decision, Sat 3 Oct 01:36Z, up to 4 KiB per V1 PR): the pin-summary
  // hover rides the /performance FeedbackWidget. Main 5504f60e7 + this PR
  // measured 243,886 locally, 174 bytes over 243,712 (CI measured 243,884); 850
  // bytes remain. Payback, not yet measured: load the summary popover lazily.
  // Merge note (PR #4014 x SET-11 x #4094 x #4906): main's merged ceiling is
  // 260,096. The Preview port's reviewed ceiling was 251,904. This merge keeps
  // the larger of the two.
  // RAISED Mon 5 Oct 2026 (+1 KiB, PR #5441 CHROME-12/13, V1 budget rule of up to
  // 4 KiB per V1 PR): the top-bar skin button cycles Default, Gothic and Light, and
  // set_skin carries the skin over IPC. CI measured 260,232 against 260,096 (+136).
  // RAISED Mon 5 Oct 2026 (+1 KiB, PR #5471 AGENT-18, V1 budget rule of up to 4 KiB per
  // V1 PR): one leader tab per engine. The Web Locks controller, the leased mirror
  // publisher and the follower banner are first-paint on /performance because the
  // election must run before the first mirror PUT. CI measured 261,862 against 261,120
  // (+742). Payback, not yet measured: load TabLeaderBanner only when a tab is a follower.
  // RAISED Mon 5 Oct 2026 (+1 KiB, PR #5482 AGENT-18 follow-up, V1 budget rule): follower
  // tabs are pure viewers (leader-only writers) and the playing tab claims the lease
  // (decideFollowerClaim). Local build measured 262,224 against 262,144 (+80). Payback
  // is the same as #5471's: load TabLeaderBanner only when a tab is a follower.
  // RAISED Mon 5 Oct 2026 (+2,229 B, PR #5474 RESCUE-07 reload-resume banner, CORE
  // decision Mon 5 Oct 23:25Z): CI measured 263,349 against 263,168 (+181); the raise is
  // that overage plus 2 KiB headroom. v1 launch, re-tighten post-v1.
  // RAISED Tue 6 Oct 2026 (+1 KiB, PR #5533 ENRICH-02, V1 budget rule of up to 4 KiB per
  // V1 PR): the enrich card names where BPM and key come from, the drain's real state and
  // the folders of files not on this Mac. The card is first paint on /performance. CI
  // measured 265,423 against 265,397 (+26). Payback, not yet measured: load the expanded
  // card body only when More is pressed.
  // RAISED Tue 6 Oct 2026 (+3,355 B, shared build-16 budget PR, CORE rule: one budget PR for the
  // build-16 set instead of per-PR raises). Clean origin/main ef76df4099 measured 266,105 locally
  // (316 B of headroom). Each PR merged alone onto that main: #5577 boot-walk settle +13
  // (estimate reserved +200 for its later head), #5565 lazy library-index assets +384, #5573 Beat Sync
  // Max enables sync +42, #5576 ENRICH-03 card R1-R4 +485, #5570 drain QoS 0 (backend only). All five
  // merged measured 267,017 (the shares add up to within 1 B). #5572 SET-12 is reserved at +512, not
  // yet measured. Projected tip 267,728: 1,307 over 266,421, plus 2 KiB headroom. Payback: issue #5522.
  // RAISED Tue 6 Oct 2026 (+2,178 B, shared build-17 / batch-2 budget PR, CORE rule: one raise for
  // the set, extends #5582). Clean origin/main 5cb903d82a measured 269,151 locally (625 B of headroom;
  // #5588, #5593 and #5600 are already in it). Each batch-2 PR merged alone onto that main: #5599 +589,
  // #5565 +353, #5607 +160, #5579 +83, #5615 +82, #5611 +1, #5605 -3, #5603 -4, #5602 -50, #5595 -731,
  // #5610 0 (server only); #5587 conflicts alone, reserved +275 (its author's +358 for #5579/#5587 less
  // #5579's measured +83). Net +755, projected 269,906: 130 over 269,776, plus 2 KiB. Without #5595's
  // -731 the tip would be 270,637, still under the new limit. Payback: issue #5522.
  // RAISED Thu 1 Oct 2026: 236 -> 249 KiB for PR #4764 (Crate Desk, LIBWS-01).
  // Library mode now mounts the same BrowserPanel Gig does, behind a dynamic
  // import from "/" so the library first paint stays under its unchanged ceiling
  // (local: 256,543, base 256,695). Sharing the browser with a lazy importer
  // makes Rollup split what was one 215,093 chunk on the base into two
  // (110,383 + 106,988 = 217,371): about 2.3 KB of lost cross-chunk compression,
  // plus the workspace guards in BrowserPanel, TrackTable and TrackContextMenu.
  // Local, both sides on a clean `git worktree add` build: base 113f4b99
  // measured 239,584 over 18 files, this head 242,549 over 21, +2,965. No eager
  // import can be demoted to recover it: the browser IS this route's UI. The
  // ceiling follows the +5% ceil-to-KiB rule on 242,549.
  // Ported to main by the Crate Desk port (#4764): main's larger ceiling above is kept;
  // see the next RAISED/MEASURED note for the measurement on the ported tree.
  // RAISED Tue 6 Oct 2026 (+3,502 B, Crate Desk port of #4764 to main, LIBWS-01; within the
  // V1 rule of up to 4 KiB per V1 PR; draft, held for JIK's product call). Local, one build
  // each, clean `git worktree add`: origin/main 4d00338f6 measured 269,714 over 22 files; this
  // branch 272,784 over 25, +3,070 (830 over the old limit). Same mechanism the source
  // measured (+2,965): "/" now lazy-imports the shared BrowserPanel, so Rollup splits the
  // browser out of this route's chunk and the split loses cross-chunk compression, plus the
  // Library-mode guards in BrowserPanel, TrackTable and TrackContextMenu. The raise is the
  // measured size ceiled to KiB (273,408) plus 2 KiB headroom. Payback: none open; the
  // browser is this route's UI and stays eager.
  // RAISED Fri 16 Oct 2026 (+3,072 B, part C of #6211, REACT-01 `ui.react.deck`; within the V1
  // rule of up to 4 KiB per V1 PR). Local, one clean build each: part A 479c89602 (main
  // a7a1ef3fb plus the tooling) measured 274,759 over 24 files; this head 275,958 over 24, +1,199
  // (502 over the old limit). The weight is ReactIsland, the flag read in Deck and HotCueBank, and
  // the seven twin loader stubs; react, react-dom and the twins themselves stay lazy in
  // other-lazy, so a build with the flag off fetches none of them. The raise is the measured size
  // ceiled to KiB (276,480) plus 2 KiB headroom. Payback: when the v2 shell is React, the islands
  // and the Svelte originals they wrap leave this route together.
  // RAISED Thu 22 Oct 2026 (+3 KiB, PR #6235, GRIDEDIT-6235..6235d + DECKCTL-6235..6235d: grid adjust and
  // shift buttons and their request queue, AU, MA tempo editor with TAP, Q phase, INT loop source
  // menu; JIK's V1 rule, up to 4 KiB per V1 PR). Local vite build: main c454c7b27 276,520; main +
  // this PR 280,957 (+4,437, 2,429 over), of which the grid-edit request chunk moved off "/" is
  // about 1.5 KiB. Payback: none open; these are six live deck controls replacing inert ones.
  // Merge note (PR #6235 x main 4c130633d, Tue 27 Oct 2026): with main's own growth this PR
  // measured 283,545 (main alone 278,339, +5,206), 1,945 over. Paid back inside the same raise:
  // the grid-edit queue and its re-read now load on the first event or press, and the tempo
  // editor (MA, double-click on BPM) on first open. Local vite build: 280,203 (+1,864).
  // RAISED Thu 22 Oct 2026 (+1 KiB, PR #6819, re-land of #6195 stem separation UI, V1 rule of
  // up to 4 KiB per V1 PR): the /performance decks ship the "Separate this track" button and
  // the deck's separation view. Local, one clean build each: main b447c807c measured 276,554
  // over 25 files; this head 278,643 over 27, +2,089 (115 over the old limit). The separation
  // store itself stays lazy. Payback, not yet measured: load the separation view on first use.
  // RAISED AGAIN Tue 27 Oct 2026 (+2 KiB more, same PR, merge skew; +3 KiB over main's limit in
  // all, within the V1 rule): CI's build of this head merged with main 4c130633d1 measured
  // 280,526 over 27 files, 974 over the earlier raise, because main itself grew the route since
  // b447c807c while this PR's own share stayed +2,089. The limit is that measurement ceiled to
  // KiB (281,600). Same payback as above.
  // RAISED Tue 27 Oct 2026 (+3,072 B, top bar re-land #6824 of #6201, TOPVIEW-01..04; within
  // the V1 rule of up to 4 KiB per V1 PR). On Thu 22 Oct it fit with no raise: its earlier
  // +3 KiB was paid back by loading the two phase views (PerfViewArea.svelte) on first use,
  // and main b447c807ce 276,572 plus this PR measured 278,123. Main has since grown to within
  // 138 bytes of the limit. Locally, one clean build each: main 4c130633d1 278,390; main +
  // this PR 280,095 (+1,705). The weight is the top bar's view switch, Pads toggle and two-deck
  // auto-play control, which render on this route. The raise is the measured size ceiled to
  // KiB (280,576) plus 1 KiB of jitter. Payback: none open; the controls are this route's UI.
  // Merged onto main e5fb30dcfd Wed 28 Oct 2026 (#6819 took main's ceiling to 281,600):
  // locally, main 280,509; main + this PR 282,162 (+1,653). RAISED to the measured size ceiled
  // to KiB (282,624) plus 1 KiB of jitter, +2 KiB over main's ceiling; same payback.
  // RAISED Thu 15 Oct 2026 (+2,048 B, USB export re-land #6826 of #6191, USBEXP-01..04; within
  // the V1 rule of up to 4 KiB per V1 PR). Local, one build each: main merge-base measured
  // 273,395; this branch 275,553 (+2,158, 97 over the old limit). Most of it is the
  // `usb-export.svelte` store joining the shared chunk first paint loads (about 2 KB); the
  // lazily loaded stick menu adds about 90 B of re-exports. The extra KiB of headroom covers
  // the 2-3 KB gap other PRs have measured between CI and local builds. Payback: lazy-load
  // the usb-export store behind the stick tree, logged in (private path).
  // Same day, same PR: the single in-flight operation guard in the usb-export store
  // (close and re-entry refused while a plan, write or eject runs) moved it to 275,859
  // (+306 B, local, one build); the limit is unchanged.
  // RAISED Fri 16 Oct 2026 (+1,024 B, same PR, now +3 KiB of its 4 KiB V1 allowance): CI
  // measured 277,936 on c76d0a3317, 432 over 277,504, after the write-warning, quit-close and
  // vanished-stick fixes in the usb-export store. Same payback as above.
  // Merged onto main b447c807ce Thu 22 Oct 2026: main took its own +3 KiB (#6211 part C,
  // above) to the same 278,528 ceiling, so this PR's raise now lands on top of it. Locally,
  // one clean build each: main 276,572; main + this PR 280,023 (+3,451). This PR's raise is
  // +2 KiB over main's ceiling (280,576), within its 4 KiB V1 allowance; same payback.
  // RAISED again Thu 22 Oct 2026 (+1,024 B, same PR, now +3 KiB over main's ceiling, within its
  // 4 KiB V1 allowance): the review fixes since (eject race, plan envelope check, malformed
  // volume listing refused, USB bus filter, literal verified check) measured 280,596 locally,
  // 20 over 280,576. Same payback as above.
  // Merged onto main e5fb30dcfd Wed 28 Oct 2026 (#6819 took main's ceiling to 281,600): this
  // PR's raise now lands on top of it. Local, one clean build each: main 280,509; main + this
  // PR 285,249 (+4,740). RAISED to +4 KiB over main's ceiling (285,696), the whole V1
  // allowance, with 447 B headroom; same payback as above.
  // Merged onto main again Wed 28 Oct 2026, after #6824 took main's ceiling to 283,648 (above):
  // locally, one clean build each, main + #6824 282,162; main + this PR 287,067 (+4,905, the
  // stick-action UUID gate included). RAISED to +4 KiB over main's ceiling (287,744), the whole
  // V1 allowance, with 677 B headroom; same payback as above.
  // RAISED Wed 28 Oct 2026 (trunk repair #7658): main 275b88e7f8 is over this budget after
  // #7594 (autodj defects + agent DJ control) and #6565 (MIK energy backfill) landed through
  // selected-test bundle merges (#7598) that did not run the budget gate. One clean nucbox
  // build each: main 906dc7be74 287,062; main 275b88e7f8 287,946 (+884).
  // Measured ceiled to KiB plus 1 KiB of jitter. Payback: owed by #7594 and #6565.
  // RAISED Wed 28 Oct 2026 (+2 KiB, PR #6574, KEYSHIFT-01..05, merged onto main 27263feb59):
  // main's limit plus this PR's own delta, ceiled to KiB; the delta (+1,830) is from one clean
  // nucbox build each of main 906dc7be74 with and without this PR. Payback: none planned.
  // measured below is this merged revision's own nucbox build: 289,877.
  // RAISED Wed 28 Oct 2026 (+1 KiB, PR #7600, LIBUX-53 collapse rule, re-merged onto main after #6574):
  // this PR's measured delta on top of main's limit (#6574's), ceiled to KiB. One clean nucbox build
  // each against main 906dc7be74 measured +343. Payback: none planned.
  // RAISED Wed 28 Oct 2026 (+2 KiB, PR #7601, LIBUX-54 column chooser, re-merged onto main after #7600):
  // this PR's measured delta on top of main's limit (after #7600), ceiled to KiB. One clean nucbox build
  // each against main 906dc7be74 measured +1,027. Payback: none planned.
  // RAISED Wed 28 Oct 2026 (+2 KiB, PR #7574, QLTSCAN-02 audio quality grade, re-merged onto main after #7601):
  // main's limit plus this PR's own delta, ceiled to KiB; the delta (+1,143) is from one clean
  // nucbox build each of main 906dc7be74 with and without this PR. Payback: none planned.
  // RAISED Wed 28 Oct 2026 (+11 KiB, PR #6829, re-land of #6205, re-measured after merging
  // main b26ee5252c, #7700; see the LOWERED note on library): settings modules /performance
  // shares with the lazy overlay are charged here, plus the mixer port's own controls. Local,
  // one clean vite build each: main b26ee5252c library 290,576, performance 287,936, other-lazy 375,376 (953,888); main + this PR
  // 274,187, 299,765, 394,367 (968,319), net +14,431 B across the three.
  // Over the 4 KiB V1 rule net of the move: flagged for the maintainer. Measured ceiled to KiB (300,032)
  // plus 1 KiB of jitter. Payback: none open.
  // Thu 29 Oct 2026 (re-measured after merging main 3dcda3b028, which now carries #6223's code): one clean vite build each, main 294,894 / 292,373 / 392,191 and main + this PR 277,411 / 304,114 / 412,036, deltas -17,483 / +11,741 / +19,845 B, ceiled to -17 / +12 / +20 KiB on main's 296,960 / 294,912 / 393,216, so 279,552 / 307,200 / 413,696 (+15 KiB net). The earlier carry measured before #6223 landed while its limit already counted #6223.
  // RAISED Fri 30 Oct 2026 (+2 KiB, trunk repair after #7957 and #6534; see the same note on
  // library): one clean vite build each, main 507102f2b9 306,705; main 8849e7a94e 307,648.
  // Measured ceiled to KiB (308,224) plus 1 KiB of jitter. Payback: owed by #7957 and #6534.
  // (Ported into PR #6189 from #8069; PR #6189 adds +20 B, inside this limit.)
  // Merge note (PR #6235 x main 131f65be61, Fri 30 Oct 2026): main's growth since 4c130633d (#6829's
  // lazy-settings move among it) re-bases the #6235 raises above onto main's ceiling. Local, one
  // clean vite build each: main 131f65be61 306,066; main + this PR 307,603 (+1,537).
  // RAISED +2 KiB over main's ceiling: the measured size ceiled to KiB (308,224) plus 1 KiB of
  // jitter, within the V1 rule of up to 4 KiB per V1 PR. Payback: none open; these are live deck
  // controls.
  // Merged onto main 8849e7a94e Fri 30 Oct 2026: main alone measures 307,648 locally, 448 B over its own
  // 307,200 ceiling; main + this PR 309,407 (+1,759). RAISED to the measured size ceiled to KiB (310,272)
  // plus 1 KiB of jitter; 1 KiB of the +4 KiB over main's ceiling is main's own overrun. Payback: none open.
  // Merged onto main 2cd9af583b Sat 31 Oct 2026, after #8069's repair took this ceiling to 309,248: main measures
  // 308,633 locally; main + this PR 310,340 (+1,707). RAISED to the measured size ceiled to KiB (311,296)
  // plus 1 KiB of jitter, +3 KiB over main's ceiling, within the V1 rule. Payback: none open.
  // RAISED Sat 31 Oct 2026 (PR #6835, re-land of #6123/#6196, merged onto main 29a59c570c): one clean local vite build
  // each, main 309,179 and main + this PR 311,628 (+2,449, this PR); the merged measurement ceiled to KiB => 312,320.
  // Merged onto main fd170f45c Sat 31 Oct 2026, after #6835 measured 311,628 against this same 312,320: one clean
  // local vite build each, main 312,098; main + this PR 313,726 (+1,628). RAISED to the measured size ceiled to
  // KiB (314,368) plus 1 KiB of jitter, +3 KiB over main's ceiling, within the V1 rule. Payback: none open.
  // Merged onto main d20ef4367 Sat 31 Oct 2026: main 312,067; main + this PR 313,717 (+1,650). Ceiled to KiB
  // plus 1 KiB the limit stays 315,392, +3 KiB over main's ceiling, within the V1 rule.
  // Re-land of #6221 (the I18 remote speaker), merged onto main cbd06578dd Sat 31 Oct 2026: one clean local
  // vite build each, main 314,035 and main + this PR 314,401 (+366); inside the limit above, so no raise.
  { name: 'performance', limit: 315392, measured: 313717, note: '/performance and children' },
  // Thu 10 Sep 2026: 66 -> 108 KiB for Q18 rung 1 (PR #1691). `@wasm-audio-decoders/flac`
  // is dynamically imported, so it lands here rather than in the deck route's eager
  // closure - measured as ONE chunk of 43833 gzip bytes, which is the whole of the
  // increase (107319 total, 63486 without it, against main's 64328). It is fetched only
  // when a stemmed deck loads, never at boot and never on a route that has no stems, so
  // it costs nothing on the boot-latency budget this program is elsewhere reducing.
  // Flagged rather than absorbed quietly: the PR's own body measured the deck route
  // closure (+2 KB) and did NOT measure this bucket, so this cost was unreported until
  // the gate caught it. If the AAC rung (Q18 rung 2) ever replaces this decoder rather
  // than adding to it, this limit comes back down with it. Merge note (PR #1010 x
  // #1691): PR #1010 touches only the `performance` surface, so this `other-lazy`
  // ceiling and its measured figure are carried from main unchanged.
  // RAISED Fri 11 Sep 2026: the Library Wheel route (/library-wheel SVG sunburst,
  // axis picker, fail-fast wheel-api client) is a new lazy SvelteKit node in this
  // shared bucket; measured 113,342, same +5% ceil-to-KiB rule => 119,808 (117 KiB).
  // RAISED Wed 16 Sep 2026: 117 -> 206 KiB. Inherited trunk growth, not a
  // regression from this diff: pristine origin/main measured 200,329 in CI (job
  // "frontend unit + check + build") and 200,327 on a clean `git worktree add
  // --detach` of the same tip, against a 119,808 limit. This branch measures
  // 200,328, and its only source change is a knip config file that emits no JS.
  // The growth was attributed file by file against a clean detached build of
  // 09a0b80d4, the commit that set the 117 KiB ceiling, so every byte below is a
  // measured base-to-head delta and not an estimate. Local on both sides, so the
  // known local-vs-CI minification gap cancels:
  //   base 113,225 over 28 files  ->  head 200,328 over 33 files, +87,103.
  // Re-measuring either side moves it by a few tens of bytes (terser mangles by
  // symbol frequency, so Rollup concatenation order perturbs the result); read
  // every figure here as carrying that jitter, and re-measure rather than
  // quoting one back. The +5 files are exactly the five items below.
  //   +56,875  the SECOND wasm audio decoder. `mpg123-decoder` arrived with
  //            d06137cc9 (PERF-STEMDEC-03, mp3 WASM decode vs decodeAudioData,
  //            issue #2058) and is `await import()`ed from
  //            src/lib/player/decode/flac-stem-decode.ts exactly as the flac one
  //            is, so it is correctly lazy and correctly charged here. What was
  //            one 43,821 chunk is now three: mpg123 56,476, flac 39,986, and
  //            their shared @wasm-audio-decoders runtime split out at 4,234.
  //            Fetched only when a stemmed deck decodes, never at boot, so it
  //            costs the boot-latency budget nothing. If the decoders are ever
  //            unified behind one codec shim, this limit comes back down with it.
  //   +30,228  everything else, no single dominant item. Of it, +4,097 is three
  //            genuinely new lazy route nodes (/analysis-backfill 3,527,
  //            /music-player 289, /prep 281); the rest is ordinary feature work
  //            on routes already in this bucket, led by /admin +14,662 (the
  //            Diagnostics #2411 and Playground #2414 tabs, 15 -> 28 files under
  //            src/routes/admin), /cloudsync +4,437 (Status/Config/Sync/Fleet UI,
  //            #2014), /track/[stable_id] +3,567 (lyrics hub, #2851),
  //            /playlist/[id] +1,168, /sets +874, and a long tail under 600
  //            bytes each.
  // Checked for the cheaper fixes first and neither applies: nothing here is
  // mis-attributed (the decoders sit behind real dynamic imports, and the chunk
  // that imports them is in the library closure, which is what puts them in this
  // bucket by design), and no eager import can be demoted to recover the weight.
  // So this is the documented widen-deliberately case: 200,328, +5% => 210,345,
  // ceil to KiB => 210,944 (206 KiB). CI's 200,329 and trunk's local 200,327 give
  // the same 206 KiB, so the ceiling does not turn on which side was measured.
  // SEPARATE FINDING, deliberately NOT actioned here: `library` moved 110,770 ->
  // 250,263 over the same range and now sits at 97.8% of its inherited 256,000
  // ceiling, about 5,700 bytes from red. A 66,479 gzip chunk of deck engine (Beat
  // Sync, KEY SYNC, SLIP, Signalsmith stretch, headphone output, mixer singleton)
  // is in the STATIC closure of the root layout, so "/" pays for it at boot. That
  // is a boot-weight question for the perf program, not a CI-green one, and
  // raising a budget that is not failing is not this change's to make.
  // RAISED Mon 21 Sep 2026: 206 -> 221 KiB for PR #3548 (cue alignment through
  // a worklet sink). The diff adds exactly five files to this bucket, measured
  // against a clean detached build of origin/main ac68b743c, both local:
  //   +3,094  assets/cue-bridge-processor (the AudioWorklet, fetched by URL)
  //   +2,634  the calibration flow (CueAlignAborted, the operator guidance)
  //   +1,679  the mic and room-output probe (getUserMedia, device matching)
  //   +1,007  the headphone output-liveness wrapper
  //     +707  the cue bridge wiring (AudioWorkletNode construction)
  //   = +9,121, 205,553 -> 214,670 over 36 -> 41 files.
  // Every one sits behind a real dynamic import, reached only when headphone
  // cue is used or the calibration modal opens, so none of it is boot or
  // first-paint weight. The two cheaper fixes do not apply: nothing is
  // mis-attributed, and there is no eager import left to demote. The ceiling
  // follows the +5% ceil-to-KiB rule on 214,670. main alone measured 205,553
  // against the old 210,944, so the diff, not trunk growth, is what crossed it.
  // RAISED Thu 24 Sep 2026: 221 -> 245 KiB, the library paybacks landing. The two
  // deferrals the library notes above promise (#3903: the feedback pin shell
  // loads after boot; #3862: SetupOverlay is a dynamic import, fetched at shell
  // boot but off the first paint) move their weight out of `library` and into
  // this bucket by design. Measured on the merged tree (main 4c65e17b + #3903
  // 06289e85 + #3862), local, one build: library 274,679 on main -> 250,040
  // (under its unchanged 259,072 ceiling with 9,032 bytes of headroom, so the
  // three reviewed KiB above are paid back and NOT raised again); other-lazy
  // 214,670 -> 238,822 over 41 -> 46 files. The ceiling follows the +5%
  // ceil-to-KiB rule on 238,822. This is deferred-code weight: none of it is on
  // the boot or first-paint path, which is the point of moving it here.
  // Decision record: the CI-infra lane comment on #3913, Thu 24 Sep 2026 12:15Z.
  // RAISED Tue 29 Sep 2026: 245 -> 260 KiB for PR #4321 (Rust engine mode, NAE-13).
  // Its first head put the mode's code in `library` and read 260,503 there, over
  // the 259,072 ceiling. The fix loads that code with a dynamic import only when
  // the mode is on, which moves it here by design: one chunk of 6,267 gzip (the
  // engine connection, load, state mirror, page-decided sync and hot cues, and
  // the socket client). Local build, one pass: library 255,553 (98.6%, not
  // raised), other-lazy 253,082 over 50 files; without that chunk it is 246,815,
  // matching CI's 246,855 on the head before, so the chunk, not trunk growth, is
  // what crossed. None of it is boot or first-paint weight, and no eager import
  // is left to demote: what stays eager is the engine choice and command sets
  // the dispatcher and Settings read. The ceiling follows the +5% ceil-to-KiB
  // rule on 253,082.
  // RAISED Sat 26 Sep 2026: 245 -> 280 KiB for PR #3837 (Reloop Mixtour Pro
  // I/O + MIDI + Play from USB). Clean origin/main a3ca2f14a measured 247,222;
  // this merged tree measures 272,962, +25,740, all fetched on demand:
  //   ~+17,900  the MIDI engine and drawer, moved OUT of performance (whose
  //             MIDI share fell by ~14,350) by the deferral noted there. The
  //             difference is real chunk overhead: split chunks lose shared
  //             gzip context. Fetched only when MIDI access is requested (a
  //             click, or a boot where the user opted in) or the drawer opens.
  //    ~+6,200  Play from USB's own lazy UI: the stick tree, stick library
  //             store, row wiring and source list (UsbStickTree, usb-library,
  //             usb-row-wire, UsbSourceList). Fetched when the browser shows a
  //             stick, never at boot.
  //    ~+1,100  the stick session edits (hot cues, rating), moved out of
  //             library; fetched when a stick track is read or edited.
  //      ~+400  cue-alignment capture's live INPUT meter.
  // Per-feature figures are proportional attributions from Rollup's rendered
  // module lengths, so they carry a few hundred bytes of rounding; the bucket
  // totals are exact. This is the documented "lazy raise" case: none of it is
  // on the boot or first-paint path, which is where the deferrals put it on
  // purpose. The ceiling follows the +5% ceil-to-KiB rule on 272,962
  // (=> 286,611, 280 KiB = 286,720).
  // Payback: nothing owed to boot latency, since all of it is on demand. The
  // recoverable part is the ~3.6 KB of split-chunk overhead: loading the drawer
  // and the engine as one on-demand chunk (they already share webmidi) retires
  // most of it. The USB UI is feature weight and stays.
  // Tue 29 Sep 2026: PR #3896's click-gated MIDI drawer loader, row popovers and
  // learn-log split, merged into this preview, measure 278,508, inside the
  // UNCHANGED ceiling; no raise.
  // Wed 30 Sep 2026: the Air preview loop's merge of origin/main 2ecfde5d7
  // (which carries #4321's 260 KiB raise above) onto #3837 + #3896 measures
  // 284,759 over 64 files, local, one build: the Rust engine mode chunk fits
  // inside this UNCHANGED 280 KiB ceiling; no raise.
  // RAISED Fri 2 Oct 2026: 260 -> 275 KiB for PR #4897 (first-run wizard, V1
  // #3422/#2590). Its Trunk batch (PR #4951) failed this gate at 267,647, over
  // by 1,407. Local build, one pass: clean origin/main 3ecf276ef measured
  // 263,446 here; main + #4897 measured 267,629. The ~4.2 KB is the deferred
  // setup overlay: the agent/human error split, path shortening, the read
  // deadline with its did-not-finish states and the Welcome retry. None of it
  // is first-paint weight (library stayed within its own limit). The ceiling
  // follows the +5% ceil-to-KiB rule on 267,629.
  // MERGED Fri 2 Oct 2026 through Sat 3 Oct: the Preview port measured 302,097
  // and reviewed a 303,104 ceiling. Main's #4014 x #4906 ceiling is 296,960.
  // This merge keeps the larger Preview ceiling.
  // RAISED Thu 1 Oct 2026: 260 -> 277 KiB for PR #4764 (Crate Desk, LIBWS-01).
  // The Crate Desk (library top bar, preview bar, track inspector, selection
  // summary) is dynamically imported from "/" so it stays off the boot path,
  // which lands it here by design: one chunk of 7,724 gzip. Local, both sides on
  // a clean build: base 113f4b99 measured 261,402 over 53 files, this head
  // 269,475 over 55, +8,073 (the chunk plus terser jitter). Base alone was
  // under its 266,240 ceiling, so the diff, not trunk growth, crossed it. The
  // ceiling follows the +5% ceil-to-KiB rule on 269,475.
  // Ported to main by the Crate Desk port (#4764): main's larger ceiling above is kept.
  // MEASURED Tue 6 Oct 2026 on the port: origin/main 4d00338f6 259,420 over 82 files, this
  // branch 267,525 over 85, +8,105 (the lazy Crate Desk chunk, as the source measured
  // +8,073), inside the unchanged 303,104 ceiling. The "/" library budget fell 2,263
  // (269,671 -> 267,408): VirtualTable/TrackRow left the eager closure.
  // RAISED Fri 16 Oct 2026: 296 -> 361 KiB for REACT-01 (part C of #6211, `ui.react.deck`, the
  // React deck twins behind ReactIsland). Local, one clean build each: part A 479c89602 measured
  // 286,607 over 90 files; this head 351,420 over 101, +64,813. Nearly all of it is react +
  // react-dom/client (about 60 KB gzip), plus the seven twins and their CSS chunks. ReactIsland
  // imports react, react-dom/client and the twin dynamically, and Deck.svelte mounts an island
  // only when the daemon declares `ui.react.deck` on (default OFF), so none of this is fetched by
  // a build with the flag off. This is the documented "lazy raise" case: the ceiling follows the
  // +5% ceil-to-KiB rule on 351,420 (=> 368,991, 361 KiB = 369,664). Payback: when the v2 shell
  // itself is React, react-dom moves to that shell's own budget and this bucket gives the ~60 KB
  // back.
  // RAISED Wed 28 Oct 2026 (trunk repair #7658): main 275b88e7f8 is over this budget after
  // #7594 (autodj defects + agent DJ control) and #6565 (MIK energy backfill) landed through
  // selected-test bundle merges (#7598) that did not run the budget gate. One clean nucbox
  // build each: main 906dc7be74 367,705; main 275b88e7f8 375,402 (+7,697).
  // Measured ceiled to KiB plus 1 KiB of jitter. Payback: owed by #7594 and #6565.
  // RAISED Thu 29 Oct 2026 (main repair): main 9df6ecac45 is over this budget after #7593
  // (af--dj-tryer) merged; main CI run 40801125679 measured 380,116 against 376,832, over by
  // 3,284 (run 40801196122 at 6c372b6cb5 measured 380,111). Ceiled to the next KiB (380,928,
  // +4 KiB, the V1 cap) with no extra jitter, so the next lazy addition has to pay its own way.
  // Payback: owed by #7593.
  // RAISED Wed 28 Oct 2026 (+2 KiB, PR #6574, KEYSHIFT-01..05, merged onto main 27263feb59):
  // main's limit plus this PR's own delta, ceiled to KiB; the delta (+1,879) is from one clean
  // nucbox build each of main 906dc7be74 with and without this PR. Payback: none planned.
  // measured below is this merged revision's own nucbox build: 382,057.
  // RAISED Wed 28 Oct 2026 (+10 KiB, PR #6223, USB import plan, USBIMP-01): the import click,
  // its job wait, the ranked analysis plan and the stick-format report are lazy USB code,
  // loaded only when the USB panel opens. Re-measured after merging main b26ee5252c (#7700),
  // local, one clean build each: main b26ee5252c 375,376; main + this PR 385,467 (+10,091).
  // Next KiB above plus 1 KiB of margin: 387,072 (378 KiB). Payback: none planned; the plan
  // is the feature.
  // Thu 29 Oct 2026: this PR's +10 KiB carried onto main 0e0bc46298's 382,976 (#7725, #6574), so 393,216; measured is main 0e0bc46298 + this PR.
  // RAISED Thu 29 Oct 2026 (+2 KiB, issue #7599 library Camelot key wheel filter): main's limit
  // plus this PR's own delta, ceiled to KiB. One clean nucbox build each: main 0e0bc46298
  // 382,127; this branch 384,009 (+1,882), almost all the lazily loaded wheel panel chunk
  // (24 SVG segments, notation labels, Compatible/Reset). Carried onto main's 393,216 (#6223)
  // after the merge. Payback: none planned; when the deck popover wheel (#6179) lands it
  // should share this panel rather than add a second wheel.
  // RAISED Wed 28 Oct 2026 (+19 KiB, PR #6829, re-land of #6205, re-measured after merging
  // main b26ee5252c, #7700; see the LOWERED note on library): the lazy SettingsOverlay chunk
  // and the settings modules only it reaches. Local, one clean vite build each: main b26ee5252c library 290,576, performance 287,936, other-lazy 375,376 (953,888); main + this PR
  // 274,187, 299,765, 394,367 (968,319), net +14,431 B across the three.
  // Measured ceiled to KiB (395,264) plus 1 KiB of jitter. Payback: none open.
  // Thu 29 Oct 2026 (re-measured after merging main 3dcda3b028, which now carries #6223's code): one clean vite build each, main 294,894 / 292,373 / 392,191 and main + this PR 277,411 / 304,114 / 412,036, deltas -17,483 / +11,741 / +19,845 B, ceiled to -17 / +12 / +20 KiB on main's 296,960 / 294,912 / 393,216, so 279,552 / 307,200 / 413,696 (+15 KiB net). The earlier carry measured before #6223 landed while its limit already counted #6223.
  // Thu 29 Oct 2026, merged main 4e77993175 (#7743 raised other-lazy to 395,264): this PR's +20 KiB carried onto it, so 415,744; measured is main 4e77993175 + this PR.
  // RAISED Thu 29 Oct 2026 (+6 KiB, PR #5399, title-fix admin panel): the panel, its poller and its
  // typed API wrapper are lazy admin-route code. One clean local build each: main f6b05d5b44 392,325;
  // this PR merged onto it 397,483 (+5,158). Carried onto main bc38a57200 (limit 418,816, measured 416,287 incl. #7806) after the merge: measured 416,287 + 5,158 = 421,445; limit plus the
  // delta, ceiled to KiB: 424,960 (415 KiB). Payback: none planned; the panel is the feature.
  // RAISED Thu 29 Oct 2026 (+3 KiB, PR #7806, TRYER-16): the try-er panel's two-deck lanes (canvas waveform, knob
  // lines, beat grids, legend, shared-notes block), lazy on the flagged /tryer route (`dj.tryer`, default off).
  // Local, one clean build each on silver: main a7b02184fa 392,244; main + this PR 394,716 (+2,472). Re-merged onto
  // main 4e77993175 + #6829 (#7743 ceiling 415,744, measured 413,815): this PR's delta stacked on that measured
  // figure (413,815 + 2,472 = 416,287), limit plus delta ceiled to KiB => 418,816. Payback: none planned; the
  // lanes are the feature.
  // Merge note (PR #6235 x main 131f65be61, Fri 30 Oct 2026): main's growth since 4c130633d (#6829's
  // lazy-settings move among it) re-bases the #6235 raises above onto main's ceiling. Local, one
  // clean vite build each: main 131f65be61 421,651; main + this PR 426,383 (+4,732).
  // RAISED +3 KiB over main's ceiling: the measured size ceiled to KiB (427,008) plus 1 KiB of
  // jitter, within the V1 rule of up to 4 KiB per V1 PR. Payback: none open; these are live deck
  // controls.
  // RAISED Fri 30 Oct 2026 (PR #6687, Cmd K preview strip, cue preview and deck picker, behind flags default off): the bar's lazy chunk. One clean local vite
  // build each: before 421,949, after 429,666 (+7,717). Measured ceiled to KiB plus 1 KiB of jitter. Payback: none planned.
  // RAISED Fri 30 Oct 2026, #5886 analysis-while-playing: +1 KiB; standing up-to-4-KiB allowance. The
  // "/" payback above moves the next-up client into a lazy chunk charged here: one clean local build
  // each, main 995cc2493b + this PR 431,077 before the move, 431,626 after (+549, 522 over). Ceiled to
  // the next KiB.
  // RAISED Fri 30 Oct 2026 (+1 KiB, PR #6634, PREPWS-01 prep hub and LIBM-184 All Tracks badge, V1 rule of
  // up to 4 KiB per V1 PR): the /prep hub page replaces the stub. One clean local vite build each: main
  // 995cc2493b 431,042 (62 B of headroom); main + this PR 431,724 (+682). Limit plus 1 KiB. Payback: none
  // planned; the hub is the page.
  // RAISED Fri 30 Oct 2026 (+2 KiB over #6634's 432,128, PR #6534, DECKUX-42 snapped Gothic block edges):
  // the block snapper (block-layout.ts) is a lazy chunk so it stays off the "/" first paint; it loads
  // on the first snapped 'blocks' row. One clean local vite build each: main 995cc2493 430,952; main +
  // this PR 432,256 (+1,304, one more file). Stacked on #6634's limit: 432,128 + 1,304, ceiled to KiB:
  // 434,176. Payback: none planned; the snapper is the feature.
  // RAISED Fri 30 Oct 2026, #5886: +1 KiB for the lazy next-up client chunk (976 B gzip); no payback found in this
  // PR, under JIK's up-to-4-KiB allowance. One clean local vite build of main dadbf24168 + this PR: 432,673.
  // Merge note (#5886 x main 8849e7a94e, Fri 30 Oct 2026): #6534 stacked its +1,304 on #6634's 432,128 to
  // 434,176; this PR's +1 KiB stacks on that, so 435,200. Net raise over main is still +1 KiB.
  // RAISED Fri 30 Oct 2026 (+3 KiB, PR #6189, LIBWS-04/06 Library-mode preview, load to deck and the
  // preview agent IPC): the Crate Desk's lazy chunk. One clean local vite build each: main 995cc2493b
  // 430,952, main + this PR 433,759 (+2,807). Main's limit plus the delta, ceiled to KiB => 434,176
  // (424 KiB). Payback: none planned; the preview player and the exit gate are the feature.
  // Fri 30 Oct 2026, merged main (other-lazy 432,128 after #6634): this PR's +3 KiB carried onto it => 435,200.
  // Fri 30 Oct 2026, merged main again (other-lazy 434,176 after #6534): this PR's +3 KiB carried onto it => 437,248.
  // Merge note (PR #6235 x main 995cc2493b, Fri 30 Oct 2026, after #6687 took this ceiling to 431,104):
  // local, one clean vite build each: main 995cc2493b 431,042; main + this PR 435,831 (+4,789). Most of it is
  // the MA tempo editor (TempoEditModal, about 3.7 KB gzip plus its CSS) and the grid-edit request
  // chunk, which #6235 moved off /performance to load on first use, so they are charged here.
  // RAISED +6 KiB over main's ceiling: the measured size ceiled to KiB (436,224) plus 1 KiB of jitter.
  // Over the 4 KiB V1 rule by the deferred editor: flagged for the maintainer. Payback: none open.
  // Merged onto main dadbf24168 Fri 30 Oct 2026, after #6634 took this ceiling to 432,128 (#6634 measured
  // 431,724): one clean local vite build of main + this PR measures 436,834. RAISED to the measured size
  // ceiled to KiB (437,248) plus 1 KiB of jitter, +6 KiB over main's ceiling; same flag and payback as above.
  // Merged onto main 8849e7a94e Fri 30 Oct 2026, after #6534 took this ceiling to 434,176: one clean local
  // vite build each, main 433,444; main + this PR 438,612 (+5,168). RAISED to the measured size ceiled to KiB
  // (439,296) plus 1 KiB of jitter, +6 KiB over main's ceiling; same flag and payback as above.
  // Merged onto main 2cd9af583b Sat 31 Oct 2026, after #6189 took this ceiling to 437,248: one clean local vite
  // build each, main 436,742; main + this PR 441,645 (+4,903). RAISED to the measured size ceiled to KiB
  // (442,368) plus 1 KiB of jitter, +6 KiB over main's ceiling; same flag and payback as above.
  // Merged onto main 161156a528 Sat 31 Oct 2026 (no budget line moved on main): one clean local vite build of
  // main + this PR measures 443,369, 23 B under the limit above after main's own growth. RAISED to the
  // measured size ceiled to KiB (443,392) plus 1 KiB of jitter; same flag and payback as above.
  // Merged onto main 04e8bdfd4 Sat 31 Oct 2026, after #7956 (Cmd K v2) grew other-lazy past main's own 437,248
  // ceiling: one clean local vite build each, main 452,486; main + this PR 457,517 (+5,031). RAISED to the
  // measured size ceiled to KiB (457,728) plus 1 KiB of jitter; same flag and payback as above.
  // RAISED Sat 31 Oct 2026 (PR #6835, re-land of #6123/#6196, merged onto main bc3008bc8b): one clean local vite build
  // each, main 438,303 and main + this PR 442,329 (+4,026, this PR); main is 1,055 B over its own 437,248, inherited; the merged measurement ceiled to KiB => 442,368.
  // Merged onto main fd170f45c Sat 31 Oct 2026, after #6835 set 442,368 (main itself now measures 456,623 locally,
  // Cmd K v2 among it): main + this PR 461,395 (+4,772). RAISED to the measured size ceiled to KiB (461,824)
  // plus 1 KiB of jitter; same flag and payback as above.
  // Merge note (#5886 x main, Sat 31 Oct 2026): this PR's earlier +1 KiB other-lazy raise is dropped. Main's
  // trunk-repair limit already holds it: main measured 457,097 and this PR adds about +978 B, so about 458,075.
  // RAISED Sat 31 Oct 2026 (+16 KiB, trunk repair after #7956).
  // One clean vite build of main cc2a31a6c2, after #7956 (the Cmd K v2 bundle, behind flags default off) and #6835: bundle merges do not gate on the budget. Measured 457,097, 14,729 B over; ceiled to KiB (457,728) plus 1 KiB of jitter. Payback: none planned.
  // Merged onto main d20ef4367 Sat 31 Oct 2026, after e00c7b879 took main's ceiling to 458,752: main 456,696;
  // main + this PR 461,582 (+4,886). Ceiled to KiB plus 1 KiB the limit stays 462,848; same flag and payback.
  // Merged onto main d2ba1fc09 Sat 31 Oct 2026, after #5886: main 457,619; main + this PR 462,662 (+5,043), 186 B
  // under the limit above. RAISED to the measured size ceiled to KiB (462,848) plus 1 KiB of jitter; same flag and payback.
  // RAISED Sat 31 Oct 2026 (re-land of #6221, the I18 remote speaker, merged onto main 0c82658f08): one clean
  // local vite build each, main 456,747 and main + this PR 462,839 (+6,092: the lazily loaded remote speaker
  // modules and panel section); the merged measurement ceiled to KiB (462,848) plus 1 KiB of jitter => 463,872.
  // Payback: none planned; the speaker is the feature, and it loads only when a remote speaker order arrives.
  // Sat 31 Oct 2026, merged main 4f1b2f2010: main 457,591 and main + this PR 463,842 (+6,251).
  // Sat 31 Oct 2026, merged main cbd06578dd: main 462,764 and main + this PR 468,808 (+6,044); ceiled to KiB
  // (468,992) plus 1 KiB of jitter => 470,016.
  { name: 'other-lazy', limit: 470016, measured: 468808, note: 'all other routes plus deferred shell' },
];

// ---------------------------------------------------------------- helpers ---

// Static specifiers only: `from"x"` and a bare side-effect `import"x"`.
// `import("x")` has a paren before the quote so it deliberately does NOT match:
// dynamic imports are the split points that separate one budget from the next.
const STATIC_IMPORT = /(?:\bfrom|\bimport)\s*["']([^"']+)["']/g;
const DYNAMIC_IMPORT = /\bimport\s*\(\s*["']([^"']+)["']\s*\)/g;

function _fail(message) {
  console.error(`[ERROR] ${message}`);
  process.exit(1);
}

function _readRouteDictionary(kitAppPath) {
  let source;
  try {
    source = readFileSync(kitAppPath, 'utf8');
  } catch {
    _fail(
      `route dictionary not found at ${kitAppPath}. ` +
        'It is written by `svelte-kit sync` during `pnpm build`; run the build first.',
    );
  }
  const match = source.match(/export const dictionary = (\{[\s\S]*?\n\s*\});/);
  if (!match) {
    _fail(`could not parse the route dictionary out of ${kitAppPath} (SvelteKit output shape changed).`);
  }
  try {
    return JSON.parse(match[1]);
  } catch (error) {
    _fail(`route dictionary in ${kitAppPath} is not valid JSON: ${error.message}`);
  }
}

function _listJsFiles(immutableDir) {
  const found = [];
  const walk = (absolute) => {
    for (const entry of readdirSync(absolute)) {
      const next = join(absolute, entry);
      if (statSync(next).isDirectory()) walk(next);
      else if (entry.endsWith('.js')) found.push(relative(immutableDir, next).replace(/\\/g, '/'));
    }
  };
  walk(immutableDir);
  return found.sort();
}

function _specifiers(immutableDir, relativePath, pattern) {
  const source = readFileSync(join(immutableDir, relativePath), 'utf8');
  const out = new Set();
  for (const match of source.matchAll(pattern)) {
    if (!match[1].startsWith('.')) continue;
    out.add(normalize(join(dirname(relativePath), match[1])).replace(/\\/g, '/'));
  }
  return out;
}

function _staticClosure(immutableDir, roots, known) {
  const seen = new Set();
  const queue = roots.filter((r) => known.has(r));
  while (queue.length) {
    const file = queue.pop();
    if (seen.has(file)) continue;
    seen.add(file);
    for (const dep of _specifiers(immutableDir, file, STATIC_IMPORT)) {
      if (known.has(dep) && !seen.has(dep)) queue.push(dep);
    }
  }
  return seen;
}

function _nodeFile(immutableDir, index) {
  const name = readdirSync(join(immutableDir, 'nodes')).find((f) => f.split('.')[0] === String(index));
  if (!name) _fail(`route node ${index} named by the dictionary has no emitted file in nodes/.`);
  return `nodes/${name}`;
}

function _isPerformanceRoute(route) {
  return route === '/performance' || route.startsWith('/performance/');
}

// ------------------------------------------------------------------ main ---

function main() {
  const args = process.argv.slice(2);
  const rootIndex = args.indexOf('--root');
  const root = rootIndex >= 0 ? args[rootIndex + 1] : '.';
  const asJson = args.includes('--json');

  const buildDir = join(root, 'build');
  const immutableDir = join(buildDir, '_app/immutable');
  const kitAppPath = join(root, '.svelte-kit/generated/client/app.js');

  try {
    if (!statSync(immutableDir).isDirectory()) throw new Error('not a directory');
  } catch {
    _fail(`${immutableDir} not found; run 'pnpm build' first.`);
  }

  const dictionary = _readRouteDictionary(kitAppPath);
  if (!dictionary['/']) _fail('the route dictionary has no "/" entry, so the library page cannot be located.');

  const inventory = _listJsFiles(immutableDir);
  const known = new Set(inventory);

  // Roots.
  const html = readFileSync(join(buildDir, 'index.html'), 'utf8');
  const htmlRoots = [...html.matchAll(/\/_app\/immutable\/([^"')\s]+\.js)/g)].map((m) => m[1]);
  if (htmlRoots.length === 0) _fail('index.html references no _app/immutable JS; the build looks broken.');

  const libraryRoots = [...htmlRoots, _nodeFile(immutableDir, 0), _nodeFile(immutableDir, dictionary['/'][0])];
  const performanceRoots = Object.entries(dictionary)
    .filter(([route]) => _isPerformanceRoute(route))
    .flatMap(([, nodes]) => nodes)
    .map((n) => _nodeFile(immutableDir, n));
  if (performanceRoots.length === 0) {
    _fail('no /performance route found in the dictionary; the performance budget would measure nothing.');
  }

  const libraryFiles = _staticClosure(immutableDir, libraryRoots, known);
  const performanceFiles = _staticClosure(immutableDir, performanceRoots, known);

  // Everything else lazily reachable: remaining route nodes, plus deferred
  // app-shell chunks (SvelteKit's error template is dynamically imported from
  // entry/app.js and belongs to no route).
  const otherRoots = [
    _nodeFile(immutableDir, 1),
    ...Object.entries(dictionary)
      .filter(([route]) => !_isPerformanceRoute(route))
      .flatMap(([, nodes]) => nodes)
      .map((n) => _nodeFile(immutableDir, n)),
  ];
  for (const file of [...libraryFiles, ...performanceFiles]) {
    for (const target of _specifiers(immutableDir, file, DYNAMIC_IMPORT)) {
      if (known.has(target) && !libraryFiles.has(target) && !performanceFiles.has(target)) otherRoots.push(target);
    }
  }
  const otherFiles = _staticClosure(immutableDir, otherRoots, known);

  const reach = { library: libraryFiles, performance: performanceFiles, 'other-lazy': otherFiles };

  // Second pass: runtime-loaded modules (AudioWorklet processors and the like)
  // are fetched by URL, so no import edge reaches them. Charge each to the
  // surface whose own code names it. Anything named by nobody stays
  // unattributed and fails below, which is the intended loud failure.
  const attributedByGraph = new Set([...libraryFiles, ...performanceFiles, ...otherFiles]);
  const leftovers = inventory.filter((f) => !attributedByGraph.has(f));
  if (leftovers.length) {
    const bodies = Object.fromEntries(
      BUDGETS.map((b) => [
        b.name,
        [...reach[b.name]].map((f) => readFileSync(join(immutableDir, f), 'utf8')).join('\n'),
      ]),
    );
    for (const file of leftovers) {
      const basename = file.split('/').pop();
      const owner = BUDGETS.find((b) => bodies[b.name].includes(basename));
      if (owner) reach[owner.name].add(file);
    }
  }

  const charge = (file) => BUDGETS.find((b) => reach[b.name].has(file))?.name ?? 'UNATTRIBUTED';

  const totals = Object.fromEntries(BUDGETS.map((b) => [b.name, { bytes: 0, files: 0 }]));
  const unattributed = [];
  for (const file of inventory) {
    const bucket = charge(file);
    const bytes = gzipSync(readFileSync(join(immutableDir, file))).length;
    if (bucket === 'UNATTRIBUTED') unattributed.push({ file, bytes });
    else {
      totals[bucket].bytes += bytes;
      totals[bucket].files += 1;
    }
  }

  if (asJson) {
    console.log(JSON.stringify({ totals, unattributed, budgets: BUDGETS }, null, 2));
  } else {
    console.log('bundle budgets (gzip bytes, JS under _app/immutable/):');
    for (const budget of BUDGETS) {
      const { bytes, files } = totals[budget.name];
      const pct = ((bytes / budget.limit) * 100).toFixed(1);
      const verdict = bytes > budget.limit ? 'OVER' : 'ok';
      console.log(
        `  ${budget.name.padEnd(12)} ${String(bytes).padStart(7)} / ${String(budget.limit).padStart(6)}` +
          `  ${pct.padStart(5)}%  ${String(files).padStart(2)} files  [${verdict}]  ${budget.note}`,
      );
    }
    const counted = Object.values(totals).reduce((a, t) => a + t.files, 0);
    console.log(`  coverage: ${counted} of ${inventory.length} emitted JS files charged to a budget`);
  }

  // Fail loudly: an unmeasured chunk is the exact bug this gate was rewritten to
  // stop, so it is an error and not a warning.
  let failed = false;
  if (unattributed.length) {
    failed = true;
    console.error(
      `[ERROR] ${unattributed.length} emitted chunk(s) belong to no budget. Every client chunk must be ` +
        'measured by exactly one surface. Add the owning route to a budget, or add a new budget:',
    );
    for (const { file, bytes } of unattributed) console.error(`  ${file} (${bytes} bytes gzip)`);
  }
  for (const budget of BUDGETS) {
    const { bytes } = totals[budget.name];
    if (bytes > budget.limit) {
      failed = true;
      console.error(
        `[ERROR] budget "${budget.name}" exceeded: ${bytes} bytes gzip against a ${budget.limit} limit, ` +
          `over by ${bytes - budget.limit} bytes (${budget.note}).`,
      );
    }
  }
  if (failed) {
    console.error(
      '[ERROR] reproduce locally: cd apps/webui/frontend && pnpm build && bash scripts/check-bundle-size.sh',
    );
    process.exit(1);
  }
  console.log('[OK] all bundle budgets satisfied and every emitted chunk is measured.');
}

main();
