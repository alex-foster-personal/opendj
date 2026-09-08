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
 * every deferred family boot-scheduler.ts actually defers (confirmed against
 * its real constants and its callers -- UserBauble/AccountOverlay auth/me,
 * BuildIdentity build-info, FeedbackWidget feedback/todos+comments+general,
 * usage-heartbeat telemetry/heartbeat, jobs-store jobs) is mocked explicitly
 * below, `PAST_BOOT_BURST_MS` correctly covers BOOT_QUIET_MS +
 * BOOT_IDLE_TIMEOUT_MS with a 1s margin (this route mounts no deck load, so
 * DECK_LOAD_YIELD_MAX_MS never applies here), and the wait runs before the
 * final assertions -- both halves confirmed present and correct, no gap
 * found. Bite proved directly rather than cited: a throwaway scratch copy of
 * the pre-#1319 shape (catch-all only, no explicit boot-scheduler mocks, no
 * wait) was run against a 4.5s artificial delay injected into the mocked
 * `/api/v1/tracks` response, standing in for a loaded CI runner stretching
 * wall-clock time past the release window without needing 15 concurrent
 * jobs on one host to do it -- it failed with exactly the deferred family
 * requests logged as "unexpected" (auth/me x2, telemetry/heartbeat,
 * feedback/todos, feedback/comments, feedback/general, build-info; jobs did
 * not fire in that run). This spec, given the identical injected delay,
 * passed. See this PR's body for the full run output; the
 * scratch files were deleted immediately after and never committed.
 * #1385 (Sun 6 Sep 2026) is a separate, later change to the same file: it
 * widened the `td.c-art` visibility timeout 15s -> 45s for a distinct
 * cold-vite-pipeline symptom on the FIRST spec the rekordbox gate runs, not
 * this catch-all/deferred-burst race. PR #1357's Sun 6 Sep failure (job
 * 101479373895, `e2e gate` attempt 1, `stretch-quality` config) was
 * confirmed from the raw CI log to be that separate cold-vite symptom --
 * `toBeVisible` timing out at exactly 15000ms on `td.c-art` with no
 * unmocked-request log at all -- already fixed by #1385, not evidence this
 * fix is incomplete. No sibling spec shares this catch-all-plus-assert-on-
 * log pattern either (re-checked repo-wide Tue 8 Sep 2026:
 * `rekordbox-writeback-disabled.spec.ts` is the only other spec with a
 * catch-all `**\/api/v1/**` route, and it fulfils permissively with no
 * equivalent assertion, so it is not a latent flake of this shape).
 */
import { expect, test } from '@playwright/test';
import { BOOT_IDLE_TIMEOUT_MS, BOOT_QUIET_MS } from '../../src/lib/rb/boot-scheduler';

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
	await page.route('**/api/v1/state/ui-mirror', (route) => route.fulfill({ json: {} }));
	await page.route('**/api/v1/feedback/performance-marks', (route) =>
		route.fulfill({ json: { count: 0, last_mark: null } })
	);
	await page.route('**/api/v1/client-events', (route) => route.fulfill({ json: {} }));
	await page.route('**/api/v1/client-errors', (route) => route.fulfill({ json: {} }));
	await page.route('**/api/v1/commands/next', (route) => route.fulfill({ status: 409, json: {} }));
	await page.route('**/api/v1/ingest/coverage', (route) =>
		route.fulfill({ json: { total_tracks: 1, on_disk: 1, unreachable: 0, missing: { vocals: 1, stems: 1 }, generated_at: 0 } })
	);
	await page.route('**/api/v1/reconcile/summary', (route) =>
		route.fulfill({ json: { total_tracks: 1, total_broken: 0, orphan_broken: 0, playlists: [] } })
	);
	await page.route('**/api/sets/recorder', (route) =>
		route.fulfill({ json: { active: false, owned: false, pid: null, recoverable: false, session_id: null } })
	);
	await page.route('**/api/v1/playlists', (route) => route.fulfill({ json: [] }));
	await page.route(/\/api\/v1\/tracks(?:\?.*)?$/, (route) =>
		route.fulfill({ json: { items: [TRACK], next_cursor: null } })
	);

	// The boot scheduler's own deferred families (boot-scheduler.ts, PERF-R6).
	// None of these are the artwork reader path this test is about; they are
	// mocked here purely so the deferred burst is a known, harmless quantity
	// rather than something the catch-all route above has to guess at.
	await page.route('**/api/v1/auth/me', (route) => route.fulfill({ status: 401, json: {} }));
	await page.route('**/api/v1/build-info', (route) => route.fulfill({ status: 404, json: {} }));
	await page.route('**/api/v1/feedback/todos', (route) => route.fulfill({ json: { todos: [] } }));
	await page.route('**/api/v1/feedback/comments', (route) => route.fulfill({ json: { comments: [] } }));
	await page.route('**/api/v1/feedback/general', (route) => route.fulfill({ status: 404, json: {} }));
	await page.route('**/api/v1/telemetry/heartbeat', (route) => route.fulfill({ status: 204, json: {} }));
	await page.route(/\/api\/v1\/jobs(?:\?.*)?$/, (route) => route.fulfill({ json: [] }));

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
	// that load; the config's 60 s test timeout still bounds the whole test.
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
