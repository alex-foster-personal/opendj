/**
 * ONE end-to-end proof that a sync-triggering control is inert, in a real
 * browser, with a real render, against a daemon in one-way import mode.
 *
 * The unit tests pin the store and the markup. What only a browser can prove
 * is the property the whole W1-D gate exists for: the operator clicks the
 * control and NOTHING leaves the page toward rekordbox.
 *
 * Every /api/v1 call is fulfilled here, so no daemon and no real library is
 * involved. The relocate apply route is deliberately routed to a handler that
 * FAILS the test if it is ever reached, rather than to a canned 403 -- a 403
 * would still mean the request was fired.
 *
 * That "no daemon" fixture has to answer PREFLIGHT-01's boot gate (#771) too:
 * the root layout renders NOTHING but the gate until `GET /api/v1/preflight`
 * returns a real `status: "pass"`, so without a preflight fixture /reconcile
 * never paints and the relocate control is unreachable. The stub below is the
 * genuine all-pass payload the daemon emits when a healthy library is
 * attached -- the same condition every other route stubbed here already
 * describes -- NOT a gate bypass. There is deliberately no skip control to
 * reach for, and inventing one in a test would be inventing the exact hole
 * the gate exists to close.
 *
 * Acceptance:
 *   - [if] the "Use this file" button renders enabled [then ⛔️]
 *   - [if] it lacks the one-way-import tooltip [then ⛔️]
 *   - [if] clicking it issues POST /api/v1/relocate/.../apply [then ⛔️]
 */
import { expect, test } from '@playwright/test';

const REFUSAL = 'sync to rekordbox disabled - one-way import only';

const BROKEN_TRACK = {
	stable_id: 'sid-1',
	title: 'Midnight Drive',
	artist: 'Tamsin Quell',
	album: 'Nightwork',
	bpm: 124,
	key: '8A',
	rating: 4,
	duration_ms: 300_000,
	original_path: '/gone/Midnight Drive.mp3',
	basename: 'Midnight Drive.mp3',
	parent_dir: '/gone',
	vendor_id: '9001',
	playlist_ids: [],
	file_exists: false,
	is_streaming: false
};

/** The daemon's own all-pass shape (apps/webui/server/preflight_checks.py):
 * four rows, every one `pass`, so `PreflightOut.status` is `pass` and the
 * boot gate clears the way it does against a healthy install. */
const PREFLIGHT_PASS = {
	status: 'pass',
	checks: [
		{ id: 'engine-alive', label: 'Engine alive', status: 'pass', detail: 'the endpoint answered', remediation: null },
		{
			id: 'state-db',
			label: 'State database',
			status: 'pass',
			detail: 'schema_meta version current (expected current)',
			remediation: null
		},
		{
			id: 'audio-access',
			label: 'Audio access',
			status: 'pass',
			detail: 'read 1 byte from a sampled track in 2ms',
			remediation: null
		},
		{ id: 'library-attached', label: 'Library attached', status: 'pass', detail: '1 tracks', remediation: null }
	]
};

const CANDIDATE = {
	path: '/Users/dj/Music/Midnight Drive.mp3',
	identity_token: 'identity-1',
	confidence: 0.97,
	signals: ['basename', 'duration', 'size'],
	triple_validated: true
};

test('the relocate control is inert and fires nothing while sync is disabled', async ({ page }) => {
	const applyAttempts: string[] = [];

	// Registered FIRST on purpose: Playwright matches routes in REVERSE
	// registration order, so the broad fallback must go down before the
	// specific handlers or it swallows every one of them.
	await page.route('**/api/v1/**', (route) => route.fulfill({ json: {} }));

	await page.route('**/api/v1/relocate/*/apply', async (route) => {
		applyAttempts.push(route.request().url());
		await route.abort();
	});

	await page.route('**/api/v1/preflight', (route) => route.fulfill({ json: PREFLIGHT_PASS }));

	await page.route('**/api/v1/rekordbox/writeback-gate', (route) =>
		route.fulfill({
			json: {
				enabled: false,
				code: 'rekordbox_writeback_disabled',
				message: 'sync to rekordbox is disabled: one-way import mode',
				ui_title: REFUSAL,
				env_var: 'MDT_REKORDBOX_WRITEBACK_ENABLED',
				surfaces: []
			}
		})
	);

	await page.route('**/api/v1/reconcile/broken*', (route) =>
		route.fulfill({ json: { total: 1, tracks: [BROKEN_TRACK] } })
	);

	await page.route('**/api/v1/relocate/candidates/*', (route) =>
		route.fulfill({
			json: {
				stable_id: BROKEN_TRACK.stable_id,
				original_path: BROKEN_TRACK.original_path,
				vendor_id: BROKEN_TRACK.vendor_id,
				total: 1,
				candidates: [CANDIDATE]
			}
		})
	);

	await page.goto('/reconcile');
	await page.getByRole('button', { name: 'Relocate' }).click();

	const useThisFile = page.getByRole('button', { name: 'Use this file' });
	await expect(useThisFile).toBeVisible();
	await expect(useThisFile).toBeDisabled();
	await expect(useThisFile).toHaveAttribute('title', REFUSAL);

	// force: a disabled button swallows a normal click, and what is under test
	// is that NO request escapes even when the control is driven anyway.
	await useThisFile.click({ force: true });
	await page.waitForTimeout(500);

	expect(applyAttempts).toEqual([]);
});
