/**
 * The API reports null, not false, when the optional artwork tag reader is
 * absent. Render the actual performance library row through the hermetic,
 * CI-gated browser harness so that distinction remains user-visible.
 *
 * Acceptance:
 *   - [if] artwork_available is null [then ⛔️] the artwork cell lacks its reader-unavailable tooltip
 *   - [if] artwork_available is null [then ⛔️] the artwork cell renders an image request
 *   - [if] the boot scheduler's deferred burst (auth/me, build-info,
 *     feedback/*, telemetry/heartbeat, jobs -- boot-scheduler.ts, PERF-R6)
 *     lands before this test's final assertions [then ⛔️] it is misread as
 *     an unexpected API call rather than the known, harmless boot chatter
 *
 * CI FLAKE, root-caused Sat 5 Sep 2026 (10 e2e-gate failures on trunk in one
 * day, 4 of them this spec): `src/lib/rb/boot-scheduler.ts` (merged Mon 1 Sep,
 * c48bdd76f/06ce393ba, well before this spec existed) defers auth/me,
 * build-info, feedback/todos+comments+general, telemetry/heartbeat and jobs
 * off the boot burst by BOOT_QUIET_MS (3s) plus an idle-frame ceiling. This
 * spec's catch-all `**\/api/v1/**` route logs anything not explicitly mocked
 * as "unexpected", so on a fast run the test finishes before the deferred
 * burst fires and the gap never shows -- on a loaded CI runner (this repo
 * runs many concurrent e2e-gate jobs on shared self-hosted agentbox
 * runners) the wall-clock race tips the other way often enough to redden
 * trunk.
 *
 * FIX APPLIED, both sides, in #1319 (Sat 5 Sep 2026) -- this is a record of
 * what IS here, not a prescription for what should be added:
 *   1. every boot-scheduler family is mocked explicitly below (auth/me,
 *      build-info, feedback/todos+comments+general, telemetry/heartbeat,
 *      jobs), so the burst is accounted for rather than merely raced
 *      against;
 *   2. `PAST_BOOT_BURST_MS` waits past the scheduler's own release window
 *      (BOOT_QUIET_MS + BOOT_IDLE_TIMEOUT_MS, imported from the scheduler
 *      itself rather than re-guessed here) before the final assertions, so
 *      the burst is always inside the observation window instead of
 *      sometimes inside it.
 * Independently re-audited line-by-line Tue 8 Sep 2026 (packet 9kb-2), not
 * trusting a prior packet's claim that both halves already landed in #1319:
 * SEVEN call sites pass a task to `bootScheduler.defer(...)` repo-wide
 * (`grep -rn 'defer(' apps/webui/frontend/src`; boot-scheduler.ts's own
 * `defer()` definition is not a caller), and all seven are listed here
 * rather than only the six that reach the network:
 *   1-2. UserBauble.svelte / AccountOverlay.svelte -> GET /api/v1/auth/me
 *   3.   BuildIdentity.svelte                      -> GET /api/v1/build-info
 *   4.   FeedbackWidget.svelte -> GET /api/v1/feedback/todos + comments +
 *        general (one deferred task, three requests)
 *   5.   usage-heartbeat.ts    -> POST /api/v1/telemetry/heartbeat
 *   6.   jobs-store.svelte.ts  -> GET /api/v1/jobs
 *   7.   deck-observer-install.ts, `deck-observer:flush` -- INERT HERE, and
 *        therefore deliberately UNMOCKED rather than overlooked. Two
 *        independent reasons, either of which alone is sufficient. (a) The
 *        deferred task calls `emitter.flushOnce()`, whose FIRST guard is
 *        `buffer.length === 0`; the sampling loop only fills that buffer
 *        while `GET /api/sets/recorder` reports `active: true`, and this
 *        spec mocks that endpoint `active: false`, so the buffer is empty
 *        and the task returns without sending. (b) Its POST target is
 *        `/api/sets/deck-observations`, which is not under `/api/v1/` and so
 *        is outside this spec's `**\/api/v1/**` catch-all in any case.
 *        A prior revision of this header claimed every deferred family was
 *        enumerated while omitting this one; that claim was false and is
 *        corrected here.
 * `PAST_BOOT_BURST_MS` correctly covers BOOT_QUIET_MS + BOOT_IDLE_TIMEOUT_MS
 * with a 1s margin (this route mounts no deck LOAD, so DECK_LOAD_YIELD_MAX_MS
 * never applies here -- the deck OBSERVER above is a different mechanism),
 * and the wait runs before the final assertions -- both halves confirmed
 * present and correct, no gap found.
 *
 * WHAT THE EVIDENCE FOR THIS FIX IS, AND WHAT IT IS NOT.
 * NOT release evidence: a throwaway scratch copy of the pre-#1319 shape
 * (catch-all only, no explicit boot-scheduler mocks, no wait) was run
 * against a 4.5s artificial delay injected into the MOCKED `/api/v1/tracks`
 * response and failed with the deferred families logged as "unexpected",
 * while this spec under the identical injected delay passed. That run is a
 * DIAGNOSTIC EXPERIMENT ONLY. It stretched wall-clock time inside a mock
 * rather than reproducing runner contention through the real runtime path,
 * and AGENTS.md L145-148 is explicit that a simulated outcome cannot be used
 * as feature or release evidence. It is recorded here because it usefully
 * localises the mechanism, and it is NOT counted as proof that the fix is
 * complete. An earlier revision of this header presented it as "bite proved
 * directly"; that framing was wrong and is withdrawn (Sol P1 on #1463).
 * WHAT IS relied on instead, all of it observed rather than simulated:
 *   - the real failures: 10 e2e-gate failures on trunk Sat 5 Sep 2026, 4 of
 *     them this spec, which is what identified the race in the first place;
 *   - a static argument over the scheduler's OWN constants, which needs no
 *     race to be checked: `PAST_BOOT_BURST_MS` is imported from
 *     boot-scheduler.ts rather than re-guessed, so it covers the release
 *     window by construction for any value those constants take, and the
 *     seven-caller census above accounts for every task the burst can fire;
 *   - the absence of recurrence on real CI since #1319.
 * No run of this spec under genuine multi-job runner contention has been
 * commissioned to demonstrate the bite, so the bite is argued from the code
 * and from the historical failures, not demonstrated on demand.
 * #1385 (Sun 6 Sep 2026) is a separate, later change to the same file: it
 * widened the `td.c-art` visibility timeout 15s -> 45s for a distinct
 * cold-vite-pipeline symptom on the FIRST spec the rekordbox gate runs, not
 * this catch-all/deferred-burst race. PR #1357's Sun 6 Sep failure (job
 * 101479373895, `e2e gate` attempt 1 of run 34030635926) was confirmed from
 * the raw attempt-1 log to be that separate cold-vite symptom --
 * `toBeVisible` timing out at exactly 15000ms on `td.c-art`, "element(s) not
 * found", with zero occurrences of "unexpected" anywhere in the job log.
 * THE CONFIG ATTRIBUTION IN AN EARLIER REVISION OF THIS HEADER WAS WRONG and
 * is corrected here (Sol P2 on #1463): that failure came from the `Root
 * Playwright suite (real engine + Vite)` step, i.e. the ROOT
 * `playwright.config.ts`, project `[chromium]`, NOT from
 * `playwright.stretch-quality.config.ts` -- whose `testMatch` is
 * 'stretch-quality.spec.ts' and which therefore cannot run this file at all.
 * The `Hermetic gate - stretch quality smoke` step in that same job
 * SUCCEEDED. NOT FIXED BY #1385 FOR THAT ROOT RUN, AND A LATER REVISION OF
 * THIS HEADER WAS WRONG TO SAY SO (Sol P2 BLOCKING on #1463, thread
 * 3961468764): the root config applies a 30-second test-level timeout
 * (`playwright.config.ts` `timeout: 30_000`), and Playwright counts time
 * spent inside the test function toward that budget, so #1385's 45-second
 * `toBeVisible` allowance is inert there -- a render finishing around 25s
 * can pass the assertion and still blow the overall 30s test timeout during
 * the unconditional `PAST_BOOT_BURST_MS` wait that follows it. This spec ran
 * TWICE per e2e-gate job because the root config's `testIgnore` list did not
 * exclude it: once under `playwright.rekordbox-gate.config.ts` (project
 * `rekordbox-gate-chromium`, 60s test timeout, where it passed in 13.1s in
 * that very job) and again under the root config's 30s budget, where it
 * failed. The actual fix (packet 9n-5) is excluding this spec from the root
 * config's `testIgnore`, so it now runs only under the rekordbox-gate config
 * that already gives it the headroom the cold-Vite pipeline needs. No
 * sibling spec shares this catch-all-plus-assert-on-
 * log pattern either (re-checked repo-wide Tue 8 Sep 2026:
 * `rekordbox-writeback-disabled.spec.ts` is the only other spec with a
 * catch-all `**\/api/v1/**` route, and it fulfils permissively with no
 * equivalent assertion, so it is not a latent flake of this shape).
 *
 * STALE-DECLARATION REGRESSION, fixed Tue 15 Sep 2026 (measured red on
 * trunk PRs #2804 and #2840, neither of which touches this file): the CI
 * log listed eight requests this file did not yet answer -- GET
 * /api/v1/playlists/deleted, /api/v1/usb/volumes (twice), /api/v1/ingest/pending,
 * /api/v1/tracks/{sid}/rb-meta (twice), /api/v1/rescue/snapshots,
 * /api/v1/perf-tier, and POST /api/v1/performance/telemetry/client-samples.
 * Re-running the same `grep -rn 'defer(' apps/webui/frontend/src` the Tue 8
 * Sep audit above used now returns twelve call sites, not seven: two new
 * deferred families reach the network and were never declared here --
 * `perf-tier:fetch` (app-init.ts) and `client-samples:first`
 * (client-performance-samples.ts), both added Sat 12 Sep 2026 (687db7217 /
 * 9f4631536), after that audit. A third new family, `machine-pressure:first`
 * (machine-pressure.ts), already had its request covered by the existing
 * /api/v1/performance/telemetry/pressure mock above, so it never showed as
 * unexpected. The other four requests are not boot-scheduler deferrals at
 * all -- they are ordinary component-mount fetches that land inside the
 * generous PAST_BOOT_BURST_MS window: LibraryNav's sidebar starts USB
 * polling on mount (usb-tracker.svelte.ts, 5s interval, hence the pair),
 * RecentlyDeletedFolder's tree constructor fetches once eagerly
 * (tree-recently-deleted.svelte.ts -> playlist-deleted.ts),
 * LibraryJobsChrome fetches ingest-pending once on mount alongside the
 * already-declared library-jobs call (ingest-pending.svelte.ts), and the
 * /performance route's own onMount calls runPerformanceRescueAutoRestore
 * (rescue-restore.svelte.ts), which lists rescue snapshots once and stops
 * there when the list is empty.
 * PERF-UI-05 playlist boot hydration (issue #3530, #3727): two KNOWN playlist
 * list reads land inside PAST_BOOT_BURST_MS and must be mocked with
 * stubPlaylistsRoute (RegExp route, not a Playwright glob on playlists alone,
 * which misses availability=skip query strings):
 *   - Boot: GET /api/v1/playlists?availability=skip (library-boot-hydration.ts)
 *   - Deferred: GET /api/v1/playlists (BrowserPanel
 *     browser-panel:refresh-playlist-availability task, bootScheduler)
 *
 * The doubled rb-meta request is NOT a duplicate-fetch defect: scrutinized
 * because the point of this spec is exactly "no artwork request for this
 * track", so a spurious second metadata fetch would have been worth
 * failing on. BrowserPanel's `_fetchRbMetaWithRetry` retries once on any
 * non-404 failure (BrowserPanel.svelte, `_hydrateRowMeta`); with the route
 * previously unmocked, the catch-all's `route.abort()` looked exactly like
 * a transient network failure, so the retry fired for real. Also confirmed
 * harmless to the assertions this spec makes: the artwork cell renders off
 * `row.artwork_available` from the /tracks list response (BrowserPanel.svelte
 * line ~1974), never off rb-meta's own artwork_available field, so mocking
 * rb-meta does not touch the artwork_available: null contract under test.
 * Declaring the route below with a normal 200 response leaves exactly one
 * rb-meta request in a manual run; the historical two-request log line
 * above is an artifact of this file having no mock, not of the app. All
 * eight requests are declared below as ordinary KNOWN requests, same
 * contract as every other declaration in this file.
 */
import { expect, test } from '@playwright/test';
import { BOOT_IDLE_TIMEOUT_MS, BOOT_QUIET_MS } from '../../src/lib/rb/boot-scheduler';

import { stubPlaylistsRoute } from './support/rekordbox-gate-playlist-routes';
import { stubUiMirrorRoutes } from './support/ui-mirror-routes';

/** Real margin past the scheduler's own quiet-period + idle-frame ceiling,
 * so the deferred burst has unquestionably landed before the final asserts
 * run -- this is what makes the wait deterministic instead of a second,
 * shorter race against the same clock. */
const PAST_BOOT_BURST_MS = BOOT_QUIET_MS + BOOT_IDLE_TIMEOUT_MS + 1_000;

const SID = 'a'.repeat(40);
const READER_UNAVAILABLE = 'artwork could not be checked (tag reader not installed in this build)';

const PREFLIGHT_PASS = {
	status: 'pass',
	checks: [
		{ id: 'engine-alive', label: 'Engine alive', status: 'pass', detail: 'the endpoint answered', remediation: null },
		{ id: 'state-db', label: 'State database', status: 'pass', detail: 'schema current', remediation: null },
		{ id: 'audio-access', label: 'Audio access', status: 'pass', detail: 'track readable', remediation: null },
		{ id: 'library-attached', label: 'Library attached', status: 'pass', detail: '1 tracks', remediation: null }
	]
};

const TRACK = {
	stable_id: SID,
	title: 'Reader Unavailable',
	artist: 'Contract Test',
	album: null,
	duration_ms: 180_000,
	bpm: 128,
	key: '8A',
	rating: 0,
	tags: [],
	notes: null,
	last_played_at: null,
	file_path: '/music/reader-unavailable.mp3',
	created_at: '2026-09-05T00:00:00Z',
	updated_at: '2026-09-05T00:00:00Z',
	provenance: {},
	preview_b64: null,
	preview_max: null,
	file_exists: true,
	// PERF-RB-01: the listing wire carries the typed status beside the bool.
	file_availability: 'present',
	quality: null,
	play_count: 0,
	vocals: { status: 'not_analyzed' },
	stems: { status: 'none' },
	has_rb_mapping: false,
	artwork_available: null,
	artwork_status: 'no_image_path'
};

test('null artwork availability identifies an unavailable reader without requesting artwork', async ({ page }) => {
	const artworkRequests: string[] = [];
	const unexpectedRequests: string[] = [];
	const pageErrors: string[] = [];
	page.on('pageerror', (error) => pageErrors.push(error.message));

	await page.route('**/api/v1/**', (route) => {
		unexpectedRequests.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`);
		return route.abort();
	});
	await page.route('**/api/v1/tracks/*/artwork**', (route) => {
		artworkRequests.push(route.request().url());
		return route.abort();
	});
	await page.route('**/api/v1/preflight', (route) => route.fulfill({ json: PREFLIGHT_PASS }));
	await page.route('**/api/v1/health', (route) =>
		route.fulfill({
			json: {
				status: 'ok',
				state_db: { tracks: 1, playlists: 0 },
				cloud: { lock_holder: null },
				bind_host: '127.0.0.1',
				syncthing: null
			}
		})
	);
	await page.route('**/api/v1/entitlements', (route) =>
		route.fulfill({
			json: {
				features: [],
				plan: { plan_id: 'local', label: 'Local', note: 'all features available', provider: null },
				provider: null,
				refusal: { ui_title: 'not included in your plan - see your account for what is included' }
			}
		})
	);
	await page.route('**/api/v1/settings', (route) =>
		route.fulfill({
			json: {
				groups: [
					{
						group: 'vibe',
						items: [
							{ key: 'vibe_sensitivity', value: 1, tbd: false },
							{ key: 'vibe_decay_per_sec', value: 0.1, tbd: false }
						]
					},
					{
						// Required at boot since #3739 (POLICY-01): the browser
						// fails fast when any runtime policy key is missing.
						group: 'Runtime policy',
						items: [
							{ key: 'hide_broken_playlist_min_available_tracks', value: 4, tbd: false },
							{ key: 'anlz_points_default', value: 38400, tbd: false },
							{ key: 'anlz_points_min', value: 100, tbd: false },
							{ key: 'anlz_points_max', value: 38400, tbd: false },
							{ key: 'file_exists_ttl_s', value: 30, tbd: false }
						]
					}
				]
			}
		})
	);
	await page.route('**/api/v1/ui-prefs', (route) =>
		route.fulfill({
			json: { theme: 'dark', hide_todo_settings: false, technically_working_animate: true, confirm: {} }
		})
	);
	await page.route('**/api/v1/smartlists', (route) => route.fulfill({ json: [] }));
	await page.route('**/api/v1/cloudsync/status', (route) =>
		route.fulfill({
			json: {
				enabled: false,
				endpoint: null,
				last_pull_at: null,
				last_push_at: null,
				last_result: null,
				reason: 'not configured',
				recent_results: [],
				rows_pending: null,
				signed_in_as: null
			}
		})
	);
	await page.route('**/api/v1/update/check', (route) =>
		route.fulfill({
			json: {
				applies_via: 'desktop shell',
				endpoint: 'https://example.invalid/update',
				platform_key: 'linux-x86_64',
				same_version_different_build: false,
				status: 'up-to-date'
			}
		})
	);
	await page.route('**/api/v1/stems/tiers', (route) => route.fulfill({ json: [] }));
	await stubUiMirrorRoutes(page);
	await page.route('**/api/v1/feedback/performance-marks', (route) =>
		route.fulfill({ json: { count: 0, last_mark: null } })
	);
	await page.route('**/api/v1/client-events', (route) => route.fulfill({ json: {} }));
	await page.route('**/api/v1/client-errors', (route) => route.fulfill({ json: {} }));
	await page.route('**/api/v1/commands/next*', (route) => route.fulfill({ status: 409, json: {} }));
	await page.route('**/api/v1/ingest/coverage**', (route) =>
		route.fulfill({ json: { total_tracks: 1, on_disk: 1, unreachable: 0, missing: { vocals: 1, stems: 1 }, generated_at: 0 } })
	);
	// TrackTable asks the engine once per page session to bring stored beatgrid
	// verdicts up to date (api-grid-flags.ts `ensureGridQualityScan`). Declared
	// as a KNOWN request; `idle` is the engine's answer when nothing is running.
	await page.route('**/api/v1/beatgrid-flags/scan', (route) =>
		route.fulfill({
			json: { state: 'idle', running_scope: null, last_result: null, last_error: null }
		})
	);
	// StemCacheHealthDot.svelte reads the stem cache's disk state on mount and
	// every 60 s. Declared as a KNOWN request, with every field the wire type
	// (StemCacheStatusOut) requires and an empty, healthy cache.
	await page.route('**/api/v1/stems/cache/status', (route) =>
		route.fulfill({
			json: {
				state: 'healthy',
				stems_dir: '/e2e/stems',
				disk_total_bytes: 1_000_000_000_000,
				disk_free_bytes: 500_000_000_000,
				floor_bytes: 20_000_000_000,
				shortfall_bytes: 0,
				cache_bytes: 0,
				budget_bytes: 480_000_000_000,
				over_budget_bytes: 0,
				bundle_count: 0,
				evictable_bundle_count: 0,
				evictable_bytes: 0,
				would_evict_count: 0,
				would_evict_bytes: 0,
				local_only_count: 0,
				local_only_bytes: 0,
				local_only_stable_ids: [],
				upload_queue_count: 0,
				protected_count: 0,
				can_rehydrate: false,
				blocked_reason: null,
				settings: {
					auto_evict: false,
					enforce_interval_s: 300,
					floor_fraction: 0.02,
					floor_gib: 20,
					max_cache_gib: null
				},
				enforcer_running: false,
				last_enforcement: null,
				last_error: null
			}
		})
	);
	await page.route('**/api/v1/reconcile/summary**', (route) =>
		route.fulfill({ json: { total_tracks: 1, total_broken: 0, orphan_broken: 0, playlists: [] } })
	);
	await page.route('**/api/sets/recorder', (route) =>
		route.fulfill({ json: { active: false, owned: false, pid: null, recoverable: false, session_id: null } })
	);
	await stubPlaylistsRoute(page, (route) => route.fulfill({ json: [] }));
	await page.route('**/api/v1/playlist-history', (route) =>
		route.fulfill({
			json: { cursor: 0, limit: 50, can_undo: false, can_redo: false, entries: [] }
		})
	);
	await page.route(/\/api\/v1\/tracks(?:\?.*)?$/, (route) =>
		route.fulfill({ json: { items: [TRACK], next_cursor: null } })
	);
	// LIBM-171 (#5549): All Tracks is now resolved from the library index, one
	// GET /tracks/index after the boot first page. Same single row, so the pane
	// still renders TRACK with artwork_available null and nothing may ask for art.
	await page.route('**/api/v1/tracks/index', (route) =>
		route.fulfill({ json: { revision: 'e2e-artwork-reader-unavailable', items: [TRACK] } })
	);
	// Wave rows gate per-track lyrics reads on this index (#1869, LYRICS-06).
	await page.route('**/api/v1/tracks/lyrics-cached-ids', (route) =>
		route.fulfill({ json: { stable_ids: [] } })
	);

	// The boot scheduler's own deferred families (boot-scheduler.ts, PERF-R6).
	// None of these are the artwork reader path this test is about; they are
	// mocked here purely so the deferred burst is a known, harmless quantity
	// rather than something the catch-all route above has to guess at.
	await page.route('**/api/v1/auth/me', (route) =>
		route.fulfill({ status: 200, json: { signed_in: false, user: null } })
	);
	await page.route('**/api/v1/build-info', (route) => route.fulfill({ status: 404, json: {} }));
	await page.route('**/api/v1/feedback/todos', (route) => route.fulfill({ json: { todos: [] } }));
	await page.route('**/api/v1/feedback/comments', (route) => route.fulfill({ json: { comments: [] } }));
	await page.route('**/api/v1/feedback/general', (route) => route.fulfill({ status: 404, json: {} }));
	await page.route('**/api/v1/telemetry/heartbeat', (route) => route.fulfill({ status: 204, json: {} }));
	await page.route(/\/api\/v1\/jobs(?:\?.*)?$/, (route) => route.fulfill({ json: [] }));
	// LibraryJobsChrome on /performance hydrates GET /api/v1/library-jobs for
	// stems and lyrics on attach (library-jobs-store.svelte.ts). Declared as a
	// KNOWN request, same contract as analysis/source above.
	await page.route('**/api/v1/library-jobs**', (route) =>
		route.fulfill({
			json: {
				lane: 'stems',
				items: [],
				counts: {}
			}
		})
	);
	// The machine-pressure poller (machine-pressure.ts): a deck-load row stamps
	// the conditions it was measured under, so the page polls this while
	// visible. Answered `available: false` because this gate cares only that
	// the request is a KNOWN one, and the unavailable branch is also the shape
	// the packaged app really gets (its payload stages `apps`, not `scripts`).
	await page.route('**/api/v1/performance/telemetry/pressure', (route) =>
		route.fulfill({ json: { available: false, reason: 'not measured in the e2e gate' } })
	);

	// PARITY-02's source toggle (AnalysisSourceToggle.svelte) mounts inside the
	// top bar of this same /performance route and polls GET
	// /api/v1/analysis/source every 5 s, so two of them land inside the
	// PAST_BOOT_BURST_MS window below. Declared here as a KNOWN request rather
	// than relaxed out of the assertion: the point of `unexpectedRequests` is
	// that every route the boot path touches is one somebody named on purpose,
	// and this poll is documented (analysis-source.svelte.ts, spec section 3).
	// The lanes payload is the daemon's launch state - every lane on its
	// persisted default with the dev toggle unset - so the toggle renders RBX
	// and never triggers a deck refresh.
	await page.route('**/api/v1/analysis/source', (route) =>
		route.fulfill({
			json: { lanes: { beatgrid: { default: 'rbx', toggle: 'unset', effective: 'rbx' } } }
		})
	);

	// perf-tier-client.ts, deferred directly in app-init.ts's
	// `startAppInstruments` (`scheduler.defer('perf-tier:fetch', ...)`, added
	// 687db7217, Sat 12 Sep 2026) rather than from its own module. Answered
	// 'STANDARD'/'auto' so it never overrides the caps the test does not care
	// about.
	await page.route('**/api/v1/perf-tier', (route) =>
		route.fulfill({
			json: { tier: 'STANDARD', source: 'auto', auto_tier: 'STANDARD', override: 'auto' }
		})
	);
	// EnrichCard.svelte (ENRICH-01) reads the enrichment summary on mount.
	// Answered `show: false`, the engine's shape for a library with nothing
	// left to enrich, so the card stays hidden and fires nothing else.
	await page.route('**/api/v1/enrich/summary', (route) =>
		route.fulfill({
			json: {
				show: false,
				analysis: null,
				analysis_error: null,
				coverage: null,
				coverage_error: null,
				stems: { state: 'done', pending: null, reason: null },
				decisions: {}
			}
		})
	);
	// preview-strip-fill.ts (NATIVE-21, LIBM-172) asks /library/row-assets for
	// the strip, vocals and cover verdict of strip-less rows in view. Answered
	// "nothing on disk yet" with the same cover verdict as the row below
	// (artwork_available null: the reader is unavailable), so no row gains a
	// waveform, the cover stays unresolved and no id is pending a retry.
	await page.route('**/api/v1/library/row-assets', (route) => {
		const { ids } = route.request().postDataJSON() as { ids: string[] };
		const none = {
			preview_b64: null,
			preview_max: null,
			vocals: { status: 'not_analyzed' },
			artwork_available: null,
			artwork_status: 'no_image_path'
		};
		return route.fulfill({ json: { assets: Object.fromEntries(ids.map((id) => [id, none])), pending: [] } });
	});
	// client-performance-samples.ts's `startClientPerformanceSampling`, also
	// deferred from app-init.ts (`scheduler.defer('client-samples:first', ...)`,
	// same 9f4631536 introduction, Sat 12 Sep 2026). The module never reads the
	// response body, so an empty 204 is the honest minimal answer.
	await page.route('**/api/v1/performance/telemetry/client-samples', (route) =>
		route.fulfill({ status: 204, json: {} })
	);
	// telemetry-consent.ts's `bootTelemetryConsent`, deferred from app-init.ts
	// (`scheduler.defer('telemetry-consent:fetch', ...)`, PR #3737, Mon 21 Sep
	// 2026, OBS-05): one GET after the boot window. Answered `declined` so the
	// consent dialog never mounts and no replay loader is fetched; the gate
	// cares only that the request is a KNOWN one.
	await page.route('**/api/v1/telemetry/consent', (route) =>
		route.fulfill({
			json: {
				decision: 'declined',
				terms_version: '2026-09-21',
				terms_current_version: '2026-09-21',
				decided_at: '2026-09-21T00:00:00Z',
				telemetry_active: false,
				environment: null,
				release: null,
				replay_loader_url: null,
				replay_session_sample_rate: 1,
				replay_on_error_sample_rate: 1
			}
		})
	);
	// RecentlyDeletedFolder.svelte constructs a TreeRecentlyDeleted on mount,
	// which fetches this eagerly in its constructor
	// (tree-recently-deleted.svelte.ts -> playlist-deleted.ts, LIBMX-03). The
	// wire type is a bare array (DeletedPlaylistOut[]), not an envelope.
	await page.route('**/api/v1/playlists/deleted', (route) => route.fulfill({ json: [] }));
	// LibraryNav.svelte starts USB polling on mount (usb-tracker.svelte.ts
	// `startUsbWatch`, LIBMX-13): one immediate fetch plus a 5s interval, which
	// is why this lands twice inside PAST_BOOT_BURST_MS.
	await page.route('**/api/v1/usb/volumes', (route) =>
		route.fulfill({ json: { volumes: [], scanned_at: 0 } })
	);
	// LibraryJobsChrome.svelte also starts the ingest-pending watch on mount,
	// alongside the already-declared library-jobs call above
	// (ingest-pending.svelte.ts `startIngestPendingWatch`, LIBMX-13). An empty
	// batches array means the store never arms its own follow-up poll.
	await page.route('**/api/v1/ingest/pending', (route) => route.fulfill({ json: { batches: [] } }));
	// The /performance route's own onMount calls runPerformanceRescueAutoRestore
	// (rescue-restore.svelte.ts, #2704). An empty snapshots list makes it
	// return immediately with no follow-up /rescue/restore call.
	await page.route('**/api/v1/rescue/snapshots', (route) =>
		route.fulfill({ json: { snapshots: [] } })
	);
	// BrowserPanel's IntersectionObserver-gated per-row hydration
	// (`_hydrateRowMeta`, BrowserPanel.svelte) fetches this for every visible
	// row, including the one row this spec renders. Declared as a real 200 so
	// `_fetchRbMetaWithRetry`'s one-retry-on-failure never fires (see the
	// STALE-DECLARATION note above for why the unmocked route logged it
	// twice). This payload's own artwork_available is unrelated to the
	// artwork_available: null under test above, which the row already carries
	// from the /tracks list response.
	await page.route(/\/api\/v1\/tracks\/[^/]+\/rb-meta$/, (route) =>
		route.fulfill({
			json: {
				stable_id: SID,
				vendor: 'local',
				vendor_id: null,
				file_exists: true,
				is_streaming: false,
				folder_path: TRACK.file_path,
				genre: null,
				artwork_available: null,
				artwork_status: 'no_image_path',
				analysis_available: false,
				beatgrid_issue: null,
				cue_count: 0,
				quality: {
					venue: null,
					label: 'Unknown',
					rank: null,
					of: 6,
					blurb: 'not measured in the e2e gate',
					kbps: null,
					container: '.mp3',
					lossless: false
				}
			}
		})
	);

	await page.goto('/performance');
	const artworkCell = page.locator('td.c-art').first();
	// This is the FIRST spec the rekordbox gate runs, so it pays vite's cold
	// module-transform pipeline for the whole /performance route; the sibling
	// spec runs warm and renders in ~4 s. On a self-hosted runner sharing its
	// host with 14 other jobs (load 36 on 16 threads) that pipeline alone took
	// 16 s from goto to the mocked /api/v1/tracks response (trunk 8d9ded38c,
	// Sun 6 Sep 2026 17:00 UTC, trace in the e2e-gate-failures artifact), and
	// a 15 s budget went red on trunk three times that day with the row
	// arriving one second late. The budget covers the cold pipeline under
	// that load; this spec is excluded from the root config's testIgnore
	// (packet 9n-5, Sol P2 BLOCKING on #1463, thread 3961468764) precisely
	// because the root config's 30s test timeout would make this 45s
	// allowance inert -- the 60s test timeout that actually bounds the whole
	// test here is playwright.rekordbox-gate.config.ts's, the only config
	// this spec now runs under.
	await expect(artworkCell).toBeVisible({ timeout: 45_000 });
	await expect(artworkCell).toHaveAttribute('title', READER_UNAVAILABLE);
	await expect(artworkCell.locator('img')).toHaveCount(0);

	// Let the boot scheduler's deferred burst land before the final asserts,
	// deterministically rather than racing it: see the CI-FLAKE note above.
	await page.waitForTimeout(PAST_BOOT_BURST_MS);

	expect(artworkRequests).toEqual([]);
	expect(unexpectedRequests).toEqual([]);
	expect(pageErrors).toEqual([]);
});
