# `library` budget history before charges

Write-once record, moved verbatim from the comment above this surface's `BUDGETS` entry in
`scripts/bundle-budget.mjs` on Tue 3 Nov 2026 (ADR-NEW-bundle-budget-charges). The last line
is the entry itself. Its limit, 301056, is this surface's base.json limit unchanged; its
recorded `measured` was 299419. New growth is a charge file, never an edit here.

```text
RAISED Mon 21 Sep 2026 (+1 KiB, PR #3737, OBS-02/OBS-05): the client error
path now carries the page's live-transport read (`any_deck_live`, so the
engine can hold Sentry sends while a deck plays) and app-init defers the
diagnostics-consent module behind the boot window. That is ~435 bytes of
genuine first-paint weight; the consent module and its dialog themselves
are dynamically imported and land in other-lazy. Clean origin/main
(17c74562) measured 256,059 locally and within 80 bytes of the limit on
CI before this PR, the same "no headroom left" state the performance
budget was in on Wed 2 Sep 2026. Payback: the next library-route weight
reduction retires this KiB, not the consent code.
RAISED Wed 23 Sep 2026 (+1 KiB, PR #3681, multiple-folder first-run setup):
SetupOverlay is imported statically by +layout.svelte, so the folder-row
list (add/remove rows, per-row check, stale-scan guard) is first-paint
weight. Clean origin/main cb401fbee measured 256,747 locally (277 bytes of
headroom); this PR measured 257,513 locally and 257,523 on CI, +766 bytes.
Payback: lazy-loading SetupOverlay (it renders only while the first-run
overlay is open) retires this KiB and the one above.
PR #3645 (DECKUX-19 stem mini-waveforms) adds +333 bytes of first-paint
weight (the show_stems pref, settings row and perf-tier cache scalers)
and lands AFTER #3681, so it takes no raise of its own: both fit under
this one KiB. Merged tree (main 97fc14795 + #3645) measured 257,887 locally;
after #3739 (main 47324919a) it measured 257,987, 61 bytes of headroom left.
RAISED Thu 24 Sep 2026 (+1 KiB, PR #3865, STEM-37 stems on installed spokes):
clean origin/main ccad1999e already measures 258,367 locally, 319 bytes
OVER this limit (merge skew: each PR since #3739 passed alone). This PR
adds +183 on top (the deck's hydrating probe state; the wait loop itself
is in its own module), 258,550 locally and on CI within 35 bytes. The
SetupOverlay payback above still retires all three KiB.
Thu 24 Sep 2026: the SetupOverlay payback (#3862) and the pin-shell deferral
(#3903) landed together; merged tree measured 250,040 locally against the
unchanged 259,072. Not raised: 9,032 bytes of headroom, first since #3737.
RAISED Sat 26 Sep 2026 (+2 KiB, PR #3837, Reloop Mixtour Pro I/O + MIDI +
Play from USB): clean origin/main a3ca2f14a measured 254,175; the PR as
written measured 260,918, +6,743. Two slices of that were not first-paint
code and are now deferred: the IOPIN-06 takeover POLICY (performance-ipc
reports the mode, so it imported the policy owner; the display state is now
the takeover-ui leaf and the policy loads with the MIDI engine) and the
stick session edits (USB-only, now imported on demand by the async
call sites that already knew the id was a stick id). Measured after:
259,325, 253 bytes over. What remains, about +5.1 KB, is engine and API
code the root layout's static closure already carries: the headphone
cluster's live signal meters and multichannel monitor routing
(headphones.ts, audio-output-topology.ts), the stem-control restore, the
takeover mode's IPC command and read-model field, the USB id routing in
api.ts/api-rb.ts/track-source.ts, and the rb.midi_enabled setting row.
Limit follows next whole KiB above the measurement plus 1 KiB of jitter
margin: 260,096 + 1,024 => 261,120 (255 KiB).
Payback: taking the deck audio engine out of the root layout's static
closure (the 66,479-byte engine chunk flagged under other-lazy's Wed 16 Sep
entry) retires these 2 KiB; the #3837 bytes above ride in that engine.
Wed 30 Sep 2026: the Air preview loop's merge of origin/main 2ecfde5d7 onto
#3837 + #3896 measures 262,092 locally, 972 bytes OVER this limit (merge
skew: main's 500 commits since the merge base add first-paint weight on
top of #3837's). Deliberately NOT raised on that local, never-pushed
branch: #3837 must pay back or justify a raise on its own rebase onto main.
RAISED Fri 2 Oct 2026 (+1 KiB, PR #4897, first-run wizard skip and plain-language
copy, SETUP-26 / UX-R2-03/04): merge skew again. Clean origin/main e0dfa83bc measured
258,429 locally; main + this PR 259,214 (142 OVER), main + this PR + its follow-up
#4928 (failed-import sentence) about 259,380. Each PR passed alone. Payback, not
yet measured: find which of this PR's setup modules the first-paint closure
reaches and move the wizard-only copy behind the lazy SetupOverlay import.
RAISED Fri 2 Oct 2026 (+2 KiB, PR #4908, CUEOUT-22 two-device headphone cue in the
Mac app): headphones.ts is in the first-paint closure, and its native-sink
branch adds +1,250 bytes even with the socket client and worker split behind a
dynamic import. Clean origin/main c8b8f5aeb measured
259,943 locally (153 bytes of headroom); main + this PR 261,193. The second KiB is
headroom for #4906 (stems on a playing deck), which lands right after with +1,319
of its own and raises once more. Payback, not yet measured: find what pulls
headphones.ts into the "/" closure and defer it.
RAISED Sat 3 Oct 2026 (+1 KiB, PR #5086, CLOUDSYNC-33 "This file isn't on this
computer"): audio-engine and performance-ipc are in the first-paint closure, and
the deck-load failure wording (deck-load-context.ts: the plain-reason map, the
error-keyed WeakMap the banner and toast read, the title fallback) adds +270
bytes. Clean origin/main 01d5b3520 measured 262,071 locally (73 bytes of
headroom); main + this PR 262,341. Trunk's queue removed it at 178 over on CI.
Payback: the headphones.ts deferral above retires this KiB too.
RAISED Sat 3 Oct 2026 (+1 KiB, PR #4014, issue #3986 library pins; JIK's V1
budget decision, Sat 3 Oct 01:36Z, up to 4 KiB per V1 PR): the pin batch's
compatible filter, pairing underline and confirm-pref plumbing reach the "/"
first-paint closure through the shared library browser. Clean origin/main
b6c894194 measured 261,672 locally; main + this PR at the merge of Sat 3 Oct
measured 262,967 (823 OVER). Payback, not yet measured: move the compatible-filter
and pairing derivation behind the /performance-only lazy imports. Re-merged with
main 5504f60e7 (which carries #5086's KiB): 263,265 measured, 927 under.
RAISED Sat 3 Oct 2026 (+1 KiB, PR #4906, STEM-44..48): the deck's named stem
states (stem-status.ts, StemRow) render on "/" and are first-paint weight. Clean
origin/main measured 262,071 locally before #5086; main + this PR 263,325, +1,254.
Already paid back inside this PR: the retry decision and the live handoff load on
first use (stem-retry.ts, stem-live-handoff.ts), -232 measured. On main 423376f7d
with #5086's own +1 KiB, main + this PR measures 263,784 locally (CI read within
20 bytes of local on this PR's last head).
Payback, not yet measured: the label table in stem-status.ts.
Merge note (PR #4014 x #4906, Sat 3 Oct 2026): both sides already sit on the
same 264,192 ceiling (the larger of the two). The merged tree measures
264,845 (653 over); +5% ceil-to-KiB => 278,528 (272 KiB).
RAISED Wed 14 Oct 2026 (+1 KiB, PR #6687 then PR #6689, CMDK-07..10, CMDK-15 and
CMDK-19, issues #6090 to #6092): #6687 adds the preview strip's one-track read
(fetchPreviewAsset in row-assets-fill.ts) and the preview flag readers, and
#6689 the bar's IPC parity (cmdk_open, cmdk_close, then cmdk_pick), validator
and executor code in performance-ipc.svelte.ts. All of it ships in the root
chunk "/" loads; the house rule that every control has IPC parity puts the
dispatcher half on first paint. The
bar itself, its ranking and its pairs stay in the lazy v2 chunk. CI read
main + #6687 at 278,538 (10 over) and main + #6689 at 278,876; #6092's
cmdk_pick lands under the same KiB. Payback, not yet measured: the
dispatcher's per-command validators table-driven instead of one branch each.
RAISED Wed 14 Oct 2026 (+1 KiB, PR #6585 follow-up, perf board row
anlz-raw-not-deep-proxy): rawAnlz (src/lib/rb/anlz-raw.ts) keeps each deck's
ANLZ payload out of the deep $state proxy, roughly 20 MB and ~100k signals per
loaded deck, and every deck ANLZ writer on "/" calls it. Measured locally with
vite build: main 427057f20 without #6585's src 278,441; main with it 278,530
(+89, 2 over; CI read 278,529 to 278,532 on the PRs it blocked).
Payback, not yet measured: the isRawAnlz probe and the brand comment are the
only parts not needed on first paint.
RAISED Fri 16 Oct 2026 (+1 KiB, PR #6786 wave zoom): 279,552 -> 280,576 for the
band cache's tiled render in render.ts and the beat-line thinning in wave-math.ts,
both on "/" through the waveform component. CI measured 280,107 on 625d26b57 (main
alone 278,530). PR #6786 adds no zoom control, glide or settings; those land in
PR 2. Rounded up to the next KiB. No second raise.
RAISED Tue 27 Oct 2026 (+2 KiB, PR #7483, key notation + Camelot rules + master
handoff, the maintainer: "merged to main please"): the library table, deck header and
inspector now print keys through one notation (camelot_musical by default) and
the K/B columns are measured in the rendered font (canvas measureText) instead of
a 7px-per-character guess. Both are first-paint table code. This PR measured
280,578 locally against 280,576. The second KiB covers the two sibling PRs of
the same request (Camelot rule checkboxes measured 280,277 alone; level-based
MASTER handoff), each of which also adds first-paint weight. Payback: none
planned; these are features on the first screen.
RAISED Tue 27 Oct 2026 (+1 KiB, PR #7559, level-based MASTER handoff): the #7483
raise covered three sibling PRs, but with #7483 and #7557 on main this PR measured
282,660 in CI against 282,624 (36 bytes over). The level estimate and the MASTER
hold are first-paint deck code. Payback: none planned.
RAISED Tue 27 Oct 2026 (+5 KiB over #7559, auto gain, AUTOGAIN-01..06, branch af--auto-gain):
the deck audio engine is in the "/" first-paint closure, so the trim stage's
auto gain (the resolved-offset reader and its checks, the per-deck store, the
AG readout under TRIM) and the two Settings rows are first-paint weight.
The pref sync is deferred behind the boot scheduler; a split of the Settings
rows into their own leaf was tried and saved nothing, because the engine
already carries the deck code into "/". Local build, one pass: clean
origin/main 80fae484d 279,682; with this change 282,190 (+2,508). Next KiB
above plus 1 KiB of jitter margin, as #3837: 283,648 (277 KiB) alone. Merged
onto main's #7483 raise (282,624, measured 280,578): 280,578 + 2,508 = 283,086,
next KiB plus 1 KiB of jitter => 284,672 (278 KiB). Review then added the
answer contract check, write versioning, the unread-pref notice and the
audibility factor: CI on 4240999 measured 285,293, next KiB plus 1 KiB of
jitter => 286,720 (280 KiB). Merged onto main's #7559 raise (283,648, measured
282,660): auto gain adds 285,293 - 280,578 = 4,715, so 287,375 estimated, next
KiB plus 1 KiB of jitter => 288,768 (282 KiB). Payback: the engine deferral named
under #3837 above carries this weight out of "/" with the rest of the engine.
RAISED Thu 22 Oct 2026 (+2 KiB, PR #6235, GRIDEDIT-6235..6235d + DECKCTL-6235..6235d deck controls;
JIK's V1 rule, up to 4 KiB per V1 PR): the dispatcher on "/" gains the grid_edit command's
validation (grid-edit-command.ts; the request half is a lazy chunk loaded on the first
press), AU parking and the phrase-quantize seek decision. Local vite build: main c454c7b27
279,744; main + this PR 281,094 (+1,350, 518 over). The second KiB came from review
(Codex/Sol, same day): the dispatcher binds each queued grid edit to its track and grid
and merges held-button repeats before the persistence-scope wait, all needed at the
first press. Local: 281,880 (+2,136). Payback: none open.
Merge note (PR #6235 x main 4c130633d, Tue 27 Oct 2026): the #6235 raise above
(+2 KiB, +2,136 measured) lands on top of main's auto gain ceiling (288,768,
measured 287,375): 288,768 + 2,048 = 290,816 (284 KiB), the same +2 KiB, no new
raise. Local vite build of the merge: 289,345. Payback: none open.
Top bar re-land #6824 of #6201 (TOPVIEW-01/-05), Tue 27 Oct 2026: no raise here. Its
Thu 22 Oct +1 KiB raise is dropped: locally, main 4c130633d1 286,471; main + this PR
288,012 (+1,541, under the limit).
RAISED Wed 28 Oct 2026 (+1 KiB, same PR, merged onto main e5fb30dcfd): locally, main
287,253; main + this PR 288,683 (+1,430), 85 B under the limit, inside CI's build jitter.
Measured ceiled to KiB (288,768) plus 1 KiB of jitter. Payback: none open.
RAISED Wed 28 Oct 2026 (trunk repair #7658): main 275b88e7f8 is over this budget after
#7594 (autodj defects + agent DJ control) and #6565 (MIK energy backfill) landed through
selected-test bundle merges (#7598) that did not run the budget gate. One clean nucbox
build each: main 906dc7be74 289,003; main 275b88e7f8 290,882 (+1,879).
Measured ceiled to KiB plus 1 KiB of jitter. Payback: owed by #7594 and #6565.
RAISED Wed 28 Oct 2026 (+2 KiB, PR #6574, KEYSHIFT-01..05, merged onto main 27263feb59):
main's limit plus this PR's own delta, ceiled to KiB; the delta (+1,549) is from one clean
nucbox build each of main 906dc7be74 with and without this PR. Payback: none planned.
measured below is this merged revision's own nucbox build: 292,315.
RAISED Wed 28 Oct 2026 (+1 KiB, PR #7600, LIBUX-53 collapse rule, re-merged onto main after #6574):
this PR's measured delta on top of main's limit (#6574's), ceiled to KiB. One clean nucbox build
each against main 906dc7be74 measured +1,023. Payback: none planned.
RAISED Wed 28 Oct 2026 (+1 KiB, PR #7601, LIBUX-54 column chooser, re-merged onto main after #7600):
this PR's measured delta on top of main's limit (after #7600), ceiled to KiB. One clean nucbox build
each against main 906dc7be74 measured +650. Payback: none planned.
LOWERED Wed 28 Oct 2026 (-17 KiB, PR #6829, re-land of #6205, re-measured after merging
main b26ee5252c, #7700): the root layout still mounts SettingsOverlay lazily
(settings-overlay-loader.ts), so the settings modules main has since added to "/" (key
notation, auto gain) move with it into performance and other-lazy. Local, one clean vite
build each: main b26ee5252c library 290,576, performance 287,936, other-lazy 375,376 (953,888); main + this PR
274,187, 299,765, 394,367 (968,319), net +14,431 B across the three.
Measured ceiled to KiB (274,432) plus 1 KiB of jitter, which also absorbs the #7658 raise
above. Payback: owed by #7594 and #6565 (the #7658 share).
Thu 29 Oct 2026 (re-measured after merging main 3dcda3b028, which now carries #6223's code): one clean vite build each, main 294,894 / 292,373 / 392,191 and main + this PR 277,411 / 304,114 / 412,036, deltas -17,483 / +11,741 / +19,845 B, ceiled to -17 / +12 / +20 KiB on main's 296,960 / 294,912 / 393,216, so 279,552 / 307,200 / 413,696 (+15 KiB net). The earlier carry measured before #6223 landed while its limit already counted #6223.
RAISED Fri 30 Oct 2026, #5886 analysis-while-playing: +1 KiB after payback of 446 B; standing
up-to-4-KiB allowance. One clean local vite build each (Node 22.22.0): main 995cc2493b 279,229;
merged with this PR 280,062 (510 over); with the next-up client (autoplay-next-up.ts) loaded on
first use through autoplay-next-up-lazy.ts 279,616 (64 over). The rest is the xrun detector's
warmup handshake and the next-up loader stubs, first-paint code. Ceiled to the next KiB.
RAISED Fri 30 Oct 2026 (+1 KiB, PR #6127, decode-on-bytes deck load and the S2 output-device
probe, V1 rule): CI build job 124242420096 at 1dc9b508d measured library 279,864, 312 B over
279,552; ceiled to KiB. Payback: none planned.
Merge note (#5886 x main 507102f2b9, Fri 30 Oct 2026): #6127 took the same +1 KiB to 280,576 as
#5886 above; both land on one ceiling. One clean local vite build of the merge: 280,458.
RAISED Fri 30 Oct 2026 (+3 KiB, trunk repair after #7957 and #6534): main 8849e7a94e is over
this budget after #7957 (autodj chooser, bundle of 7) and #6534 (Gothic block snapping)
landed through bundle merges, which do not gate on the budget (Tue 27 Oct 2026). One clean
vite build each: main 507102f2b9 (#6127, before both) 279,920; main 8849e7a94e 282,240.
Measured ceiled to KiB (282,624) plus 1 KiB of jitter. Payback: owed by #7957 and #6534.
(Ported into PR #6189 from #8069; PR #6189 adds +421 B, inside this limit.)
Merge note (PR #6235 x main 131f65be61, Fri 30 Oct 2026): main's growth since 4c130633d (#6829's
lazy-settings move among it) re-bases the #6235 raises above onto main's ceiling. Local, one
clean vite build each: main 131f65be61 279,204; main + this PR 281,710 (+2,506).
RAISED +4 KiB over main's ceiling: the measured size ceiled to KiB (282,624) plus 1 KiB of
jitter, within the V1 rule of up to 4 KiB per V1 PR. Payback: none open; these are live deck
controls.
Merged onto main 9ec96943b0 Fri 30 Oct 2026, after #6127 took this ceiling to 280,576: one clean local
vite build of main + this PR measures 282,379. RAISED to the measured size ceiled to KiB (282,624)
plus 1 KiB of jitter, +3 KiB over main's ceiling, within the V1 rule. Payback: none open.
Merged onto main 2729743c1 Fri 30 Oct 2026: main itself measures 282,234 locally, 1,658 B over its own
280,576 ceiling (no raise landed with that growth). Main + this PR measures 284,862 (+2,628 for #6235).
RAISED to the measured size ceiled to KiB (285,696) plus 1 KiB of jitter; of the +6 KiB over main's
ceiling, 2 KiB is main's own overrun, which is owed by whichever main change grew "/".
Merged onto main 01b93d8b1 Sat 31 Oct 2026: main itself measures 285,226 locally, 4,650 B over its own
280,576 ceiling (#8069 carries main's repair). Main + this PR measures 287,887 (+2,661 for #6235).
RAISED to the measured size ceiled to KiB (288,768) plus 1 KiB of jitter; of the +9 KiB over main's
ceiling, 5 KiB is main's own overrun, owed by whichever main change grew "/".
Merged onto main 2cd9af583b Sat 31 Oct 2026, after #8069's repair took this ceiling to 283,648: main itself
measures 285,424 locally, 1,776 B over it again; main + this PR 288,027 (+2,603). The measured size
ceiled to KiB (288,768) plus 1 KiB of jitter keeps the limit at 289,792. Payback: none open.
Merged onto main 04e8bdfd4 Sat 31 Oct 2026, after #7956 (Cmd K v2): main itself measures 287,169 locally,
3,521 B over its 283,648 ceiling; main + this PR 289,659 (+2,490), 133 B under the limit above. RAISED to
the measured size ceiled to KiB (289,792) plus 1 KiB of jitter. Payback: none open.
RAISED Sat 31 Oct 2026 (PR #6835, re-land of #6123/#6196, merged onto main 29a59c570c): one clean local vite build
each, main 285,328 and main + this PR 288,777 (+3,449, this PR, #8092 port included); main is 1,680 B over its own 283,648, inherited; the merged measurement ceiled to KiB => 289,792.
Merged onto main fd170f45c Sat 31 Oct 2026, after #6835's re-land: one clean local vite build each, main
290,361 (569 B over its own 289,792); main + this PR 293,017 (+2,656). RAISED to the measured size ceiled
to KiB (293,888) plus 1 KiB of jitter. Payback: none open.
RAISED Sat 31 Oct 2026 (+2 KiB, trunk repair after #7956).
One clean vite build of main cc2a31a6c2, after #7956 (the Cmd K v2 bundle, behind flags default off) and #6835: bundle merges do not gate on the budget. Measured 289,990, 198 B over; ceiled to KiB (290,816) plus 1 KiB of jitter. Payback: none planned.
Merged onto main d20ef4367 Sat 31 Oct 2026, after e00c7b879 took main's ceiling to 291,840: main 290,409;
main + this PR 293,053 (+2,644). Ceiled to KiB plus 1 KiB the limit stays 294,912.
RAISED Mon 2 Nov 2026 (+1 KiB, headroom repair, no feature): main sat 335 B under this limit, so unrelated
PRs tipped it at random. CI frontend job 125490304350 (main 8f3c5ac827) measured 294,352; one clean nucbox vite
build each, 8f3c5ac827 294,350 and main 9a89085862 294,575 (+225), so main on CI is 294,577. Measured ceiled to KiB
(294,912) plus 1 KiB of jitter => 295,936. Payback: none planned.
RAISED Mon 2 Nov 2026 (+1 KiB, headroom repair 2, no feature): main sat 971 B under this limit after the
repair above. CI frontend job 125573392771 (main 1ac118b2f7) measured 294,965. Measured ceiled to KiB
(295,936) plus 1 KiB of jitter => 296,960. Payback: none planned.
RAISED Mon 2 Nov 2026 (+2 KiB, deck preview port, PR #7691, merged main b1cdc8056c): one clean vite build each,
main 294,955 and main + this PR 296,699 (+1,744), ceiled to +2 KiB on main's 295,936, so 297,984. Payback: none planned.
RAISED Tue 3 Nov 2026 (fold-notice follow-up #7662, merged main 2dd433890d): CI frontend job 125667086112 (head
61631aaf93) measured 299,419. Main's own CI job 125662168293 (main 1da0577f16) measured 299,240, so this PR adds
+179 B. Measured ceiled to KiB (300,032) plus 1 KiB of jitter => 301,056. Payback: none planned.
{ name: 'library', limit: 301056, measured: 299419, note: 'initial load of "/"' }
```

REBASELINED Tue 3 Nov 2026 (main 9a76938d0e, PR #8464): one clean vite build of main measured 304,540. Measured ceiled to KiB plus 1 KiB => 306,176 (was base 301,056 + charges 3,072 from #6561, #7892 = 304,128); those charges are folded into the base and deleted. Main alone was 412 B over its limit, which failed every PR's frontend job.

REBASELINED Thu 5 Nov 2026 (trunk repair, main 92a114192d): CI frontend job 127221268821 (run 42019847721) measured 324,612, 4 B over base 315,392 + charges 9,216 (#6529, #8728, #8801) = 324,608, which failed `ci gate` on main. Main 46a46568d0 measured 324,170 on CI; the +442 B is #8868 (autoplay heard key, `src/lib/rb/auto-play*`, no charge): one local vite build each of 46a46568d0, 7e395ae5e0 (#8872) and d88ece8f98 (#8868) measured 323,801, 323,801 and 324,190 (+389 B, all of it #8868). The same source measured 324,597 on CI at c56f7bec23, so the surface sat within build jitter of its limit. Main 86084188b4 (#8869, run 42023794899, frontend job 127238947586) then measured 324,629, 21 B over. That tip measurement, ceiled to KiB plus 1 KiB => 326,656; the three library charges are folded into the base and deleted. Payback: none planned.

REBASELINED Thu 5 Nov 2026 (preventive, main 20a77a0b3e, run 42045526507, frontend job 127309376521): CI measured 324,631 B gzip, 99.4% of base 326,656 (no charges), 2,025 B left. Preventive, measured + 2% until #8534 (per-PR delta plus nightly ratchet) lands: 324,631 x 1.02 = 331,123.6 => ceiled to the next KiB 331,776 (was 326,656, +5,120). Same policy as the performance and other-lazy rebaselines of Thu 5 Nov 2026 (#8908). Payback: none planned.
