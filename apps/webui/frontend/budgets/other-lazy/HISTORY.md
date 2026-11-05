# `other-lazy` budget history before charges

Write-once record, moved verbatim from the comment above this surface's `BUDGETS` entry in
`scripts/bundle-budget.mjs` on Tue 3 Nov 2026 (ADR-NEW-bundle-budget-charges). The last line
is the entry itself. Its limit, 482304, is this surface's base.json limit unchanged; its
recorded `measured` was 480663. New growth is a charge file, never an edit here.

```text
Thu 10 Sep 2026: 66 -> 108 KiB for Q18 rung 1 (PR #1691). `@wasm-audio-decoders/flac`
is dynamically imported, so it lands here rather than in the deck route's eager
closure - measured as ONE chunk of 43833 gzip bytes, which is the whole of the
increase (107319 total, 63486 without it, against main's 64328). It is fetched only
when a stemmed deck loads, never at boot and never on a route that has no stems, so
it costs nothing on the boot-latency budget this program is elsewhere reducing.
Flagged rather than absorbed quietly: the PR's own body measured the deck route
closure (+2 KB) and did NOT measure this bucket, so this cost was unreported until
the gate caught it. If the AAC rung (Q18 rung 2) ever replaces this decoder rather
than adding to it, this limit comes back down with it. Merge note (PR #1010 x
#1691): PR #1010 touches only the `performance` surface, so this `other-lazy`
ceiling and its measured figure are carried from main unchanged.
RAISED Fri 11 Sep 2026: the Library Wheel route (/library-wheel SVG sunburst,
axis picker, fail-fast wheel-api client) is a new lazy SvelteKit node in this
shared bucket; measured 113,342, same +5% ceil-to-KiB rule => 119,808 (117 KiB).
RAISED Wed 16 Sep 2026: 117 -> 206 KiB. Inherited trunk growth, not a
regression from this diff: pristine origin/main measured 200,329 in CI (job
"frontend unit + check + build") and 200,327 on a clean `git worktree add
--detach` of the same tip, against a 119,808 limit. This branch measures
200,328, and its only source change is a knip config file that emits no JS.
The growth was attributed file by file against a clean detached build of
09a0b80d4, the commit that set the 117 KiB ceiling, so every byte below is a
measured base-to-head delta and not an estimate. Local on both sides, so the
known local-vs-CI minification gap cancels:
  base 113,225 over 28 files  ->  head 200,328 over 33 files, +87,103.
Re-measuring either side moves it by a few tens of bytes (terser mangles by
symbol frequency, so Rollup concatenation order perturbs the result); read
every figure here as carrying that jitter, and re-measure rather than
quoting one back. The +5 files are exactly the five items below.
  +56,875  the SECOND wasm audio decoder. `mpg123-decoder` arrived with
           d06137cc9 (PERF-STEMDEC-03, mp3 WASM decode vs decodeAudioData,
           issue #2058) and is `await import()`ed from
           src/lib/player/decode/flac-stem-decode.ts exactly as the flac one
           is, so it is correctly lazy and correctly charged here. What was
           one 43,821 chunk is now three: mpg123 56,476, flac 39,986, and
           their shared @wasm-audio-decoders runtime split out at 4,234.
           Fetched only when a stemmed deck decodes, never at boot, so it
           costs the boot-latency budget nothing. If the decoders are ever
           unified behind one codec shim, this limit comes back down with it.
  +30,228  everything else, no single dominant item. Of it, +4,097 is three
           genuinely new lazy route nodes (/analysis-backfill 3,527,
           /music-player 289, /prep 281); the rest is ordinary feature work
           on routes already in this bucket, led by /admin +14,662 (the
           Diagnostics #2411 and Playground #2414 tabs, 15 -> 28 files under
           src/routes/admin), /cloudsync +4,437 (Status/Config/Sync/Fleet UI,
           #2014), /track/[stable_id] +3,567 (lyrics hub, #2851),
           /playlist/[id] +1,168, /sets +874, and a long tail under 600
           bytes each.
Checked for the cheaper fixes first and neither applies: nothing here is
mis-attributed (the decoders sit behind real dynamic imports, and the chunk
that imports them is in the library closure, which is what puts them in this
bucket by design), and no eager import can be demoted to recover the weight.
So this is the documented widen-deliberately case: 200,328, +5% => 210,345,
ceil to KiB => 210,944 (206 KiB). CI's 200,329 and trunk's local 200,327 give
the same 206 KiB, so the ceiling does not turn on which side was measured.
SEPARATE FINDING, deliberately NOT actioned here: `library` moved 110,770 ->
250,263 over the same range and now sits at 97.8% of its inherited 256,000
ceiling, about 5,700 bytes from red. A 66,479 gzip chunk of deck engine (Beat
Sync, KEY SYNC, SLIP, Signalsmith stretch, headphone output, mixer singleton)
is in the STATIC closure of the root layout, so "/" pays for it at boot. That
is a boot-weight question for the perf program, not a CI-green one, and
raising a budget that is not failing is not this change's to make.
RAISED Mon 21 Sep 2026: 206 -> 221 KiB for PR #3548 (cue alignment through
a worklet sink). The diff adds exactly five files to this bucket, measured
against a clean detached build of origin/main ac68b743c, both local:
  +3,094  assets/cue-bridge-processor (the AudioWorklet, fetched by URL)
  +2,634  the calibration flow (CueAlignAborted, the operator guidance)
  +1,679  the mic and room-output probe (getUserMedia, device matching)
  +1,007  the headphone output-liveness wrapper
    +707  the cue bridge wiring (AudioWorkletNode construction)
  = +9,121, 205,553 -> 214,670 over 36 -> 41 files.
Every one sits behind a real dynamic import, reached only when headphone
cue is used or the calibration modal opens, so none of it is boot or
first-paint weight. The two cheaper fixes do not apply: nothing is
mis-attributed, and there is no eager import left to demote. The ceiling
follows the +5% ceil-to-KiB rule on 214,670. main alone measured 205,553
against the old 210,944, so the diff, not trunk growth, is what crossed it.
RAISED Thu 24 Sep 2026: 221 -> 245 KiB, the library paybacks landing. The two
deferrals the library notes above promise (#3903: the feedback pin shell
loads after boot; #3862: SetupOverlay is a dynamic import, fetched at shell
boot but off the first paint) move their weight out of `library` and into
this bucket by design. Measured on the merged tree (main 4c65e17b + #3903
06289e85 + #3862), local, one build: library 274,679 on main -> 250,040
(under its unchanged 259,072 ceiling with 9,032 bytes of headroom, so the
three reviewed KiB above are paid back and NOT raised again); other-lazy
214,670 -> 238,822 over 41 -> 46 files. The ceiling follows the +5%
ceil-to-KiB rule on 238,822. This is deferred-code weight: none of it is on
the boot or first-paint path, which is the point of moving it here.
Decision record: the CI-infra lane comment on #3913, Thu 24 Sep 2026 12:15Z.
RAISED Tue 29 Sep 2026: 245 -> 260 KiB for PR #4321 (Rust engine mode, NAE-13).
Its first head put the mode's code in `library` and read 260,503 there, over
the 259,072 ceiling. The fix loads that code with a dynamic import only when
the mode is on, which moves it here by design: one chunk of 6,267 gzip (the
engine connection, load, state mirror, page-decided sync and hot cues, and
the socket client). Local build, one pass: library 255,553 (98.6%, not
raised), other-lazy 253,082 over 50 files; without that chunk it is 246,815,
matching CI's 246,855 on the head before, so the chunk, not trunk growth, is
what crossed. None of it is boot or first-paint weight, and no eager import
is left to demote: what stays eager is the engine choice and command sets
the dispatcher and Settings read. The ceiling follows the +5% ceil-to-KiB
rule on 253,082.
RAISED Sat 26 Sep 2026: 245 -> 280 KiB for PR #3837 (Reloop Mixtour Pro
I/O + MIDI + Play from USB). Clean origin/main a3ca2f14a measured 247,222;
this merged tree measures 272,962, +25,740, all fetched on demand:
  ~+17,900  the MIDI engine and drawer, moved OUT of performance (whose
            MIDI share fell by ~14,350) by the deferral noted there. The
            difference is real chunk overhead: split chunks lose shared
            gzip context. Fetched only when MIDI access is requested (a
            click, or a boot where the user opted in) or the drawer opens.
   ~+6,200  Play from USB's own lazy UI: the stick tree, stick library
            store, row wiring and source list (UsbStickTree, usb-library,
            usb-row-wire, UsbSourceList). Fetched when the browser shows a
            stick, never at boot.
   ~+1,100  the stick session edits (hot cues, rating), moved out of
            library; fetched when a stick track is read or edited.
     ~+400  cue-alignment capture's live INPUT meter.
Per-feature figures are proportional attributions from Rollup's rendered
module lengths, so they carry a few hundred bytes of rounding; the bucket
totals are exact. This is the documented "lazy raise" case: none of it is
on the boot or first-paint path, which is where the deferrals put it on
purpose. The ceiling follows the +5% ceil-to-KiB rule on 272,962
(=> 286,611, 280 KiB = 286,720).
Payback: nothing owed to boot latency, since all of it is on demand. The
recoverable part is the ~3.6 KB of split-chunk overhead: loading the drawer
and the engine as one on-demand chunk (they already share webmidi) retires
most of it. The USB UI is feature weight and stays.
Tue 29 Sep 2026: PR #3896's click-gated MIDI drawer loader, row popovers and
learn-log split, merged into this preview, measure 278,508, inside the
UNCHANGED ceiling; no raise.
Wed 30 Sep 2026: the Air preview loop's merge of origin/main 2ecfde5d7
(which carries #4321's 260 KiB raise above) onto #3837 + #3896 measures
284,759 over 64 files, local, one build: the Rust engine mode chunk fits
inside this UNCHANGED 280 KiB ceiling; no raise.
RAISED Fri 2 Oct 2026: 260 -> 275 KiB for PR #4897 (first-run wizard, V1
#3422/#2590). Its Trunk batch (PR #4951) failed this gate at 267,647, over
by 1,407. Local build, one pass: clean origin/main 3ecf276ef measured
263,446 here; main + #4897 measured 267,629. The ~4.2 KB is the deferred
setup overlay: the agent/human error split, path shortening, the read
deadline with its did-not-finish states and the Welcome retry. None of it
is first-paint weight (library stayed within its own limit). The ceiling
follows the +5% ceil-to-KiB rule on 267,629.
MERGED Fri 2 Oct 2026 through Sat 3 Oct: the Preview port measured 302,097
and reviewed a 303,104 ceiling. Main's #4014 x #4906 ceiling is 296,960.
This merge keeps the larger Preview ceiling.
RAISED Thu 1 Oct 2026: 260 -> 277 KiB for PR #4764 (Crate Desk, LIBWS-01).
The Crate Desk (library top bar, preview bar, track inspector, selection
summary) is dynamically imported from "/" so it stays off the boot path,
which lands it here by design: one chunk of 7,724 gzip. Local, both sides on
a clean build: base 113f4b99 measured 261,402 over 53 files, this head
269,475 over 55, +8,073 (the chunk plus terser jitter). Base alone was
under its 266,240 ceiling, so the diff, not trunk growth, crossed it. The
ceiling follows the +5% ceil-to-KiB rule on 269,475.
Ported to main by the Crate Desk port (#4764): main's larger ceiling above is kept.
MEASURED Tue 6 Oct 2026 on the port: origin/main 4d00338f6 259,420 over 82 files, this
branch 267,525 over 85, +8,105 (the lazy Crate Desk chunk, as the source measured
+8,073), inside the unchanged 303,104 ceiling. The "/" library budget fell 2,263
(269,671 -> 267,408): VirtualTable/TrackRow left the eager closure.
RAISED Fri 16 Oct 2026: 296 -> 361 KiB for REACT-01 (part C of #6211, `ui.react.deck`, the
React deck twins behind ReactIsland). Local, one clean build each: part A 479c89602 measured
286,607 over 90 files; this head 351,420 over 101, +64,813. Nearly all of it is react +
react-dom/client (about 60 KB gzip), plus the seven twins and their CSS chunks. ReactIsland
imports react, react-dom/client and the twin dynamically, and Deck.svelte mounts an island
only when the daemon declares `ui.react.deck` on (default OFF), so none of this is fetched by
a build with the flag off. This is the documented "lazy raise" case: the ceiling follows the
+5% ceil-to-KiB rule on 351,420 (=> 368,991, 361 KiB = 369,664). Payback: when the v2 shell
itself is React, react-dom moves to that shell's own budget and this bucket gives the ~60 KB
back.
RAISED Wed 28 Oct 2026 (trunk repair #7658): main 275b88e7f8 is over this budget after
#7594 (autodj defects + agent DJ control) and #6565 (MIK energy backfill) landed through
selected-test bundle merges (#7598) that did not run the budget gate. One clean nucbox
build each: main 906dc7be74 367,705; main 275b88e7f8 375,402 (+7,697).
Measured ceiled to KiB plus 1 KiB of jitter. Payback: owed by #7594 and #6565.
RAISED Thu 29 Oct 2026 (main repair): main 9df6ecac45 is over this budget after #7593
(af--dj-tryer) merged; main CI run 40801125679 measured 380,116 against 376,832, over by
3,284 (run 40801196122 at 6c372b6cb5 measured 380,111). Ceiled to the next KiB (380,928,
+4 KiB, the V1 cap) with no extra jitter, so the next lazy addition has to pay its own way.
Payback: owed by #7593.
RAISED Wed 28 Oct 2026 (+2 KiB, PR #6574, KEYSHIFT-01..05, merged onto main 27263feb59):
main's limit plus this PR's own delta, ceiled to KiB; the delta (+1,879) is from one clean
nucbox build each of main 906dc7be74 with and without this PR. Payback: none planned.
measured below is this merged revision's own nucbox build: 382,057.
RAISED Wed 28 Oct 2026 (+10 KiB, PR #6223, USB import plan, USBIMP-01): the import click,
its job wait, the ranked analysis plan and the stick-format report are lazy USB code,
loaded only when the USB panel opens. Re-measured after merging main b26ee5252c (#7700),
local, one clean build each: main b26ee5252c 375,376; main + this PR 385,467 (+10,091).
Next KiB above plus 1 KiB of margin: 387,072 (378 KiB). Payback: none planned; the plan
is the feature.
Thu 29 Oct 2026: this PR's +10 KiB carried onto main 0e0bc46298's 382,976 (#7725, #6574), so 393,216; measured is main 0e0bc46298 + this PR.
RAISED Thu 29 Oct 2026 (+2 KiB, issue #7599 library Camelot key wheel filter): main's limit
plus this PR's own delta, ceiled to KiB. One clean nucbox build each: main 0e0bc46298
382,127; this branch 384,009 (+1,882), almost all the lazily loaded wheel panel chunk
(24 SVG segments, notation labels, Compatible/Reset). Carried onto main's 393,216 (#6223)
after the merge. Payback: none planned; when the deck popover wheel (#6179) lands it
should share this panel rather than add a second wheel.
RAISED Wed 28 Oct 2026 (+19 KiB, PR #6829, re-land of #6205, re-measured after merging
main b26ee5252c, #7700; see the LOWERED note on library): the lazy SettingsOverlay chunk
and the settings modules only it reaches. Local, one clean vite build each: main b26ee5252c library 290,576, performance 287,936, other-lazy 375,376 (953,888); main + this PR
274,187, 299,765, 394,367 (968,319), net +14,431 B across the three.
Measured ceiled to KiB (395,264) plus 1 KiB of jitter. Payback: none open.
Thu 29 Oct 2026 (re-measured after merging main 3dcda3b028, which now carries #6223's code): one clean vite build each, main 294,894 / 292,373 / 392,191 and main + this PR 277,411 / 304,114 / 412,036, deltas -17,483 / +11,741 / +19,845 B, ceiled to -17 / +12 / +20 KiB on main's 296,960 / 294,912 / 393,216, so 279,552 / 307,200 / 413,696 (+15 KiB net). The earlier carry measured before #6223 landed while its limit already counted #6223.
Thu 29 Oct 2026, merged main 4e77993175 (#7743 raised other-lazy to 395,264): this PR's +20 KiB carried onto it, so 415,744; measured is main 4e77993175 + this PR.
RAISED Thu 29 Oct 2026 (+6 KiB, PR #5399, title-fix admin panel): the panel, its poller and its
typed API wrapper are lazy admin-route code. One clean local build each: main f6b05d5b44 392,325;
this PR merged onto it 397,483 (+5,158). Carried onto main bc38a57200 (limit 418,816, measured 416,287 incl. #7806) after the merge: measured 416,287 + 5,158 = 421,445; limit plus the
delta, ceiled to KiB: 424,960 (415 KiB). Payback: none planned; the panel is the feature.
RAISED Thu 29 Oct 2026 (+3 KiB, PR #7806, TRYER-16): the try-er panel's two-deck lanes (canvas waveform, knob
lines, beat grids, legend, shared-notes block), lazy on the flagged /tryer route (`dj.tryer`, default off).
Local, one clean build each on silver: main a7b02184fa 392,244; main + this PR 394,716 (+2,472). Re-merged onto
main 4e77993175 + #6829 (#7743 ceiling 415,744, measured 413,815): this PR's delta stacked on that measured
figure (413,815 + 2,472 = 416,287), limit plus delta ceiled to KiB => 418,816. Payback: none planned; the
lanes are the feature.
Merge note (PR #6235 x main 131f65be61, Fri 30 Oct 2026): main's growth since 4c130633d (#6829's
lazy-settings move among it) re-bases the #6235 raises above onto main's ceiling. Local, one
clean vite build each: main 131f65be61 421,651; main + this PR 426,383 (+4,732).
RAISED +3 KiB over main's ceiling: the measured size ceiled to KiB (427,008) plus 1 KiB of
jitter, within the V1 rule of up to 4 KiB per V1 PR. Payback: none open; these are live deck
controls.
RAISED Fri 30 Oct 2026 (PR #6687, Cmd K preview strip, cue preview and deck picker, behind flags default off): the bar's lazy chunk. One clean local vite
build each: before 421,949, after 429,666 (+7,717). Measured ceiled to KiB plus 1 KiB of jitter. Payback: none planned.
RAISED Fri 30 Oct 2026, #5886 analysis-while-playing: +1 KiB; standing up-to-4-KiB allowance. The
"/" payback above moves the next-up client into a lazy chunk charged here: one clean local build
each, main 995cc2493b + this PR 431,077 before the move, 431,626 after (+549, 522 over). Ceiled to
the next KiB.
RAISED Fri 30 Oct 2026 (+1 KiB, PR #6634, PREPWS-01 prep hub and LIBM-184 All Tracks badge, V1 rule of
up to 4 KiB per V1 PR): the /prep hub page replaces the stub. One clean local vite build each: main
995cc2493b 431,042 (62 B of headroom); main + this PR 431,724 (+682). Limit plus 1 KiB. Payback: none
planned; the hub is the page.
RAISED Fri 30 Oct 2026 (+2 KiB over #6634's 432,128, PR #6534, DECKUX-42 snapped Gothic block edges):
the block snapper (block-layout.ts) is a lazy chunk so it stays off the "/" first paint; it loads
on the first snapped 'blocks' row. One clean local vite build each: main 995cc2493 430,952; main +
this PR 432,256 (+1,304, one more file). Stacked on #6634's limit: 432,128 + 1,304, ceiled to KiB:
434,176. Payback: none planned; the snapper is the feature.
RAISED Fri 30 Oct 2026, #5886: +1 KiB for the lazy next-up client chunk (976 B gzip); no payback found in this
PR, under JIK's up-to-4-KiB allowance. One clean local vite build of main dadbf24168 + this PR: 432,673.
Merge note (#5886 x main 8849e7a94e, Fri 30 Oct 2026): #6534 stacked its +1,304 on #6634's 432,128 to
434,176; this PR's +1 KiB stacks on that, so 435,200. Net raise over main is still +1 KiB.
RAISED Fri 30 Oct 2026 (+3 KiB, PR #6189, LIBWS-04/06 Library-mode preview, load to deck and the
preview agent IPC): the Crate Desk's lazy chunk. One clean local vite build each: main 995cc2493b
430,952, main + this PR 433,759 (+2,807). Main's limit plus the delta, ceiled to KiB => 434,176
(424 KiB). Payback: none planned; the preview player and the exit gate are the feature.
Fri 30 Oct 2026, merged main (other-lazy 432,128 after #6634): this PR's +3 KiB carried onto it => 435,200.
Fri 30 Oct 2026, merged main again (other-lazy 434,176 after #6534): this PR's +3 KiB carried onto it => 437,248.
Merge note (PR #6235 x main 995cc2493b, Fri 30 Oct 2026, after #6687 took this ceiling to 431,104):
local, one clean vite build each: main 995cc2493b 431,042; main + this PR 435,831 (+4,789). Most of it is
the MA tempo editor (TempoEditModal, about 3.7 KB gzip plus its CSS) and the grid-edit request
chunk, which #6235 moved off /performance to load on first use, so they are charged here.
RAISED +6 KiB over main's ceiling: the measured size ceiled to KiB (436,224) plus 1 KiB of jitter.
Over the 4 KiB V1 rule by the deferred editor: flagged for the maintainer. Payback: none open.
Merged onto main dadbf24168 Fri 30 Oct 2026, after #6634 took this ceiling to 432,128 (#6634 measured
431,724): one clean local vite build of main + this PR measures 436,834. RAISED to the measured size
ceiled to KiB (437,248) plus 1 KiB of jitter, +6 KiB over main's ceiling; same flag and payback as above.
Merged onto main 8849e7a94e Fri 30 Oct 2026, after #6534 took this ceiling to 434,176: one clean local
vite build each, main 433,444; main + this PR 438,612 (+5,168). RAISED to the measured size ceiled to KiB
(439,296) plus 1 KiB of jitter, +6 KiB over main's ceiling; same flag and payback as above.
Merged onto main 2cd9af583b Sat 31 Oct 2026, after #6189 took this ceiling to 437,248: one clean local vite
build each, main 436,742; main + this PR 441,645 (+4,903). RAISED to the measured size ceiled to KiB
(442,368) plus 1 KiB of jitter, +6 KiB over main's ceiling; same flag and payback as above.
Merged onto main 161156a528 Sat 31 Oct 2026 (no budget line moved on main): one clean local vite build of
main + this PR measures 443,369, 23 B under the limit above after main's own growth. RAISED to the
measured size ceiled to KiB (443,392) plus 1 KiB of jitter; same flag and payback as above.
Merged onto main 04e8bdfd4 Sat 31 Oct 2026, after #7956 (Cmd K v2) grew other-lazy past main's own 437,248
ceiling: one clean local vite build each, main 452,486; main + this PR 457,517 (+5,031). RAISED to the
measured size ceiled to KiB (457,728) plus 1 KiB of jitter; same flag and payback as above.
RAISED Sat 31 Oct 2026 (PR #6835, re-land of #6123/#6196, merged onto main bc3008bc8b): one clean local vite build
each, main 438,303 and main + this PR 442,329 (+4,026, this PR); main is 1,055 B over its own 437,248, inherited; the merged measurement ceiled to KiB => 442,368.
Merged onto main fd170f45c Sat 31 Oct 2026, after #6835 set 442,368 (main itself now measures 456,623 locally,
Cmd K v2 among it): main + this PR 461,395 (+4,772). RAISED to the measured size ceiled to KiB (461,824)
plus 1 KiB of jitter; same flag and payback as above.
Merge note (#5886 x main, Sat 31 Oct 2026): this PR's earlier +1 KiB other-lazy raise is dropped. Main's
trunk-repair limit already holds it: main measured 457,097 and this PR adds about +978 B, so about 458,075.
RAISED Sat 31 Oct 2026 (+16 KiB, trunk repair after #7956).
One clean vite build of main cc2a31a6c2, after #7956 (the Cmd K v2 bundle, behind flags default off) and #6835: bundle merges do not gate on the budget. Measured 457,097, 14,729 B over; ceiled to KiB (457,728) plus 1 KiB of jitter. Payback: none planned.
Merged onto main d20ef4367 Sat 31 Oct 2026, after e00c7b879 took main's ceiling to 458,752: main 456,696;
main + this PR 461,582 (+4,886). Ceiled to KiB plus 1 KiB the limit stays 462,848; same flag and payback.
Merged onto main d2ba1fc09 Sat 31 Oct 2026, after #5886: main 457,619; main + this PR 462,662 (+5,043), 186 B
under the limit above. RAISED to the measured size ceiled to KiB (462,848) plus 1 KiB of jitter; same flag and payback.
RAISED Sat 31 Oct 2026 (re-land of #6221, the I18 remote speaker, merged onto main 0c82658f08): one clean
local vite build each, main 456,747 and main + this PR 462,839 (+6,092: the lazily loaded remote speaker
modules and panel section); the merged measurement ceiled to KiB (462,848) plus 1 KiB of jitter => 463,872.
Payback: none planned; the speaker is the feature, and it loads only when a remote speaker order arrives.
Sat 31 Oct 2026, merged main 4f1b2f2010: main 457,591 and main + this PR 463,842 (+6,251).
Sat 31 Oct 2026, merged main cbd06578dd: main 462,764 and main + this PR 468,808 (+6,044); ceiled to KiB
(468,992) plus 1 KiB of jitter => 470,016.
RAISED Mon 2 Nov 2026 (+2 KiB, PR #7691 deck preview port, merged main b1cdc8056c): main itself measures 470,633,
617 B over its own 470,016; main + this PR 471,082 (+449, the preset runner's unsynced-start check among it).
Ceiled to KiB, 472,064. Of the +2 KiB, 617 B is main's own overrun, owed by whichever main change grew the lazy
routes. Payback: none planned.
RAISED Mon 2 Nov 2026 (+2 KiB, PR #5917 deck hover link pref and edge tick): merged main 8283eeab3 on this
branch measures 472,686 locally (one clean vite build), 622 B over the 472,064 above. Main 8283eeab3 itself
measures 472,534, 470 B over its own limit, so this PR's own delta is about +152 B. Ceiled to KiB (473,088)
plus 1 KiB of jitter => 474,112. Payback: the main overrun above, owed by whichever main change grew the lazy routes.
RAISED Mon 2 Nov 2026 (+4 KiB, headroom repair, no feature): main went 682 B OVER this limit, so every
frontend PR read red. CI frontend job 125573392771 (main 1ac118b2f7) measured 470,698. Plus 1,903 B held for
PR #7896 (Transitions plan-overlay chunk, measured locally against main 13515f0285: 469,567 to 471,470), so
#7896 lands without touching this line: 472,601, ceiled to KiB (473,088) plus 1 KiB of jitter => 474,112.
Payback: none planned.
Mon 2 Nov 2026 (PR #7896 merged onto main with #7691, whose lazy routes grew past the bytes held above): one clean
local vite build measured 475,291, 1,179 B over. The measured size ceiled to KiB (476,160) plus 1 KiB of jitter => 477,184.
RAISED Tue 3 Nov 2026 (fold-notice follow-up #7662, merged main 2dd433890d): CI job 125667086112 (head 61631aaf93)
measured 480,663. Main's CI job 125662168293 (main 1da0577f16) measured 480,645, so this PR adds +18 B. Measured
ceiled to KiB (481,280) plus 1 KiB of jitter => 482,304. Payback: none planned.
{ name: 'other-lazy', limit: 482304, measured: 480663, note: 'all other routes plus deferred shell' }
```

REBASELINED Thu 5 Nov 2026 (preventive rebaseline, surface within 3% of ceiling, Thu 5 Nov 2026; main 86084188b4, #8869, run 42023794899, frontend job 127238947586): CI measured 485,775 B gzip, 97.2% of base 482,304 + charges 17,408 (6529, 6555, 8342, 8801) = 499,712 (99.8% on an earlier run, 888 B left). Measured ceiled to KiB (486,400) plus 1 KiB => 487,424 by the README rule, superseded by measured plus 2% (485,775 x 1.02 = 495,490 => 495,616); the four charges are folded into the base and deleted. Payback: none planned. Headroom note: the README's "next KiB plus 1 KiB" rule leaves headroom below build noise on large surfaces (local other-lazy read 486,102 against CI 485,775; library tipped on 15 B of noise on Thu 5 Nov 2026), so this preventive rebaseline uses CI measured plus 2%, rounded up to the next KiB: 485,775 x 1.02 = 495,490 => 495,616.
