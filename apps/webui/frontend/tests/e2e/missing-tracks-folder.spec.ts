/**
 * Hermetic /performance proof of the reserved Missing Tracks folder (issue #173).
 *
 * Stubs every /api/v1 call so this gate needs no daemon and no real library.
 * Repair stays on CLI / /reconcile (issue #174); this spec only lists.
 */
import { expect, type Page, test } from '@playwright/test';

import { stubPlaylistsRoute } from './support/rekordbox-gate-playlist-routes';

const PREFS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';

const PREFLIGHT_PASS = {
	status: 'pass',
	checks: [
		{ id: 'engine-alive', label: 'Engine alive', status: 'pass', detail: 'the endpoint answered', remediation: null },
		{ id: 'state-db', label: 'State database', status: 'pass', detail: 'schema current', remediation: null },
		{ id: 'audio-access', label: 'Audio access', status: 'pass', detail: 'track readable', remediation: null },
		{ id: 'library-attached', label: 'Library attached', status: 'pass', detail: '3 tracks', remediation: null }
	]
};

const USER_MISSING_PLAYLIST = {
	playlist_id: 'pl-user-missing',
	name: 'Missing Tracks',
	track_count: 2,
	available_count: 2,
	updated_at: '2026-09-11T00:00:00Z',
	vendor: 'rekordbox'
};

const BROKEN_TRACKS = [
	{
		stable_id: 'b'.repeat(40),
		title: 'Broken Alpha',
		artist: 'Ghost',
		album: null,
		basename: 'alpha.mp3',
		parent_dir: '/missing',
		original_path: '/missing/alpha.mp3',
		key: '8A',
		bpm: 128,
		rating: 0,
		duration_ms: 180_000,
		file_exists: false,
		is_streaming: false,
		playlist_ids: [],
		vendor_id: null
	},
	{
		stable_id: 'c'.repeat(40),
		title: 'Broken Beta',
		artist: 'Absent',
		album: null,
		basename: 'beta.mp3',
		parent_dir: '/missing',
		original_path: '/missing/beta.mp3',
		key: '9A',
		bpm: 130,
		rating: 0,
		duration_ms: 200_000,
		file_exists: false,
		is_streaming: false,
		playlist_ids: [],
		vendor_id: '42'
	}
];

async function stubPerformanceApis(
	page: Page,
	options: { playlistsFail?: boolean } = {}
): Promise<void> {
	await page.route('**/api/v1/**', (route) => route.abort());
	await page.route('**/api/v1/preflight', (route) => route.fulfill({ json: PREFLIGHT_PASS }));
	await page.route('**/api/v1/health', (route) =>
		route.fulfill({
			json: {
				status: 'ok',
				state_db: { tracks: 3, playlists: 1 },
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
		route.fulfill({
			json: { total_tracks: 3, on_disk: 1, unreachable: 2, missing: { vocals: 1, stems: 1 }, generated_at: 0 }
		})
	);
	await page.route('**/api/v1/reconcile/summary', (route) =>
		route.fulfill({ json: { total_tracks: 3, total_broken: 2, orphan_broken: 1, playlists: [] } })
	);
	await page.route('**/api/v1/reconcile/broken**', (route) =>
		route.fulfill({ json: { total: 2, tracks: BROKEN_TRACKS } })
	);
	await page.route('**/api/sets/recorder', (route) =>
		route.fulfill({ json: { active: false, owned: false, pid: null, recoverable: false, session_id: null } })
	);
	await stubPlaylistsRoute(page, (route) => {
		if (options.playlistsFail) {
			return route.fulfill({ status: 500, json: { detail: 'playlist boot failed (e2e)' } });
		}
		const fast = route.request().url().includes('availability=skip');
		const playlist = fast
			? { ...USER_MISSING_PLAYLIST, available_count: -1 }
			: USER_MISSING_PLAYLIST;
		return route.fulfill({ json: [playlist] });
	});
	await page.route(/\/api\/v1\/tracks(?:\?.*)?$/, (route) =>
		route.fulfill({ json: { items: [], next_cursor: null } })
	);
	await page.route('**/api/v1/tracks/lyrics-cached-ids', (route) =>
		route.fulfill({ json: { stable_ids: [] } })
	);
	await page.route('**/api/v1/tracks/*/rb-meta**', (route) =>
		route.fulfill({
			json: {
				file_exists: false,
				is_streaming: false,
				genre: null,
				artwork_available: false,
				has_anlz: false
			}
		})
	);
	await page.route('**/api/v1/auth/me', (route) =>
		route.fulfill({ status: 200, json: { signed_in: false, user: null } })
	);
	await page.route('**/api/v1/build-info', (route) => route.fulfill({ status: 404, json: {} }));
	await page.route('**/api/v1/feedback/todos', (route) => route.fulfill({ json: { todos: [] } }));
	await page.route('**/api/v1/feedback/comments', (route) => route.fulfill({ json: { comments: [] } }));
	await page.route('**/api/v1/feedback/general', (route) => route.fulfill({ status: 404, json: {} }));
	await page.route('**/api/v1/telemetry/heartbeat', (route) => route.fulfill({ status: 204, json: {} }));
	await page.route(/\/api\/v1\/jobs(?:\?.*)?$/, (route) => route.fulfill({ json: [] }));
	await page.route('**/api/v1/performance/telemetry/pressure', (route) =>
		route.fulfill({ json: { available: false, reason: 'not measured in the e2e gate' } })
	);
	await page.route('**/api/v1/analysis/source', (route) =>
		route.fulfill({
			json: { lanes: { beatgrid: { default: 'rbx', toggle: 'unset', effective: 'rbx' } } }
		})
	);
}

test('Missing Tracks folder shows the broken count and is not a user playlist', async ({ page }) => {
	const brokenRequests: string[] = [];
	page.on('request', (request) => {
		const url = request.url();
		if (request.method() === 'GET' && url.includes('/api/v1/reconcile/broken')) {
			brokenRequests.push(url);
		}
	});
	await stubPerformanceApis(page);
	await page.goto('/performance');

	const folder = page.getByTestId('playlist-missing-tracks');
	await expect(folder).toBeVisible({ timeout: 45_000 });
	await expect(folder).toContainText('Missing Tracks');
	await expect(folder.locator('.count')).toHaveText('2');

	const userRow = page.getByTestId('playlist-row').filter({ hasText: 'Missing Tracks' });
	await expect(userRow).toBeVisible();
	await expect(userRow).not.toHaveAttribute('data-testid', 'playlist-missing-tracks');

	await folder.click();
	await expect(page.locator('.title-text', { hasText: 'Broken Alpha' })).toBeVisible();
	await expect(page.locator('.title-text', { hasText: 'Broken Beta' })).toBeVisible();
	expect(brokenRequests.some((url) => !url.includes('playlist_id'))).toBeTruthy();
});

test('Hide broken links does not empty the Missing Tracks folder or its rows', async ({ page }) => {
	await page.addInitScript((prefsKey) => {
		window.localStorage.setItem(prefsKey, JSON.stringify({ hide_broken_links: true }));
	}, PREFS_STORAGE_KEY);
	await stubPerformanceApis(page);
	await page.goto('/performance');

	const folder = page.getByTestId('playlist-missing-tracks');
	await expect(folder).toBeVisible({ timeout: 45_000 });
	await expect(folder.locator('.count')).toHaveText('2');

	await folder.click();
	await expect(page.locator('.title-text', { hasText: 'Broken Alpha' })).toBeVisible();
	await expect(page.locator('.title-text', { hasText: 'Broken Beta' })).toBeVisible();
});

test('Missing Tracks count still resolves when playlist boot fails (#3750)', async ({ page }) => {
	// [if] BrowserPanel._init() rejects because the playlist read fails [then] the
	// reconcile summary still loads from its finally block and the Missing Tracks
	// count renders, [else stop]. Drives the real component; only the API is stubbed.
	const reconcileRequests: string[] = [];
	page.on('request', (request) => {
		if (request.url().includes('/api/v1/reconcile/summary')) reconcileRequests.push(request.url());
	});
	await stubPerformanceApis(page, { playlistsFail: true });
	await page.goto('/performance');

	await expect(page.getByText(/browser init failed/)).toBeVisible({ timeout: 45_000 });
	const folder = page.getByTestId('playlist-missing-tracks');
	await expect(folder).toBeVisible({ timeout: 45_000 });
	await expect(folder.locator('.count')).toHaveText('2');
	expect(reconcileRequests.length).toBeGreaterThan(0);
});
