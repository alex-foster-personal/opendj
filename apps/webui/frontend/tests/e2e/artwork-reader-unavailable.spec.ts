/**
 * The API reports null, not false, when the optional artwork tag reader is
 * absent. Render the actual performance library row through the hermetic,
 * CI-gated browser harness so that distinction remains user-visible.
 *
 * Acceptance:
 *   - [if] artwork_available is null [then ⛔️] the artwork cell lacks its reader-unavailable tooltip
 *   - [if] artwork_available is null [then ⛔️] the artwork cell renders an image request
 */
import { expect, test } from '@playwright/test';

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

	await page.goto('/performance');
	const artworkCell = page.locator('td.c-art').first();
	await expect(artworkCell).toBeVisible({ timeout: 15_000 });
	await expect(artworkCell).toHaveAttribute('title', READER_UNAVAILABLE);
	await expect(artworkCell.locator('img')).toHaveCount(0);
	expect(artworkRequests).toEqual([]);
	expect(unexpectedRequests).toEqual([]);
	expect(pageErrors).toEqual([]);
});
