// requirement: PERFMODE-15
// [if] Trackify route is opened from the mode chooser [then] the listening shell renders
// [if] a Trackify load fails [then] exactly one toast shows, the skip toast (#4036)
//   [⛔️ if the engine's own load-failure toast for Deck 1 shows as well, or none]
// [if] that engine toast is muted for Trackify [then] the real backend still stores
//   exactly one deck-load report carrying deck and stage_failedAt
//   [⛔️ if muting the toast drops the report]
// [if] an ordinary (non-Trackify) load fails [then] the engine toast still shows,
//   once, with the reason in its headline (pin a4898e22), and reports once, and
//   its message names the error code once
//   [⛔️ if the fix muted the engine toast for every caller]
// Everything here runs against the real engine daemon and fixture library: the
// 404 is the production track route's, and each report is read back from the
// production /api/v1/client-errors response, never fabricated.

import { expect, test } from '@playwright/test';

const MISSING_STABLE_ID = 'trackify-e2e-missing-stable-id';

type DeckLoadReport = { message: string; context: Record<string, unknown>; stored: boolean };
type ReportTracker = { deckLoad: DeckLoadReport[]; sent: number; answered: number };

const CLIENT_ERROR_QUEUE_KEY = 'music-dj-tools:client-errors:v1';

function _isClientErrorPost(request: import('@playwright/test').Request): boolean {
	return request.method() === 'POST' && request.url().includes('/api/v1/client-errors');
}

/** Every deck-load client-error report the page sends, with the real backend's answer. */
function _recordDeckLoadReports(page: import('@playwright/test').Page): ReportTracker {
	const tracker: ReportTracker = { deckLoad: [], sent: 0, answered: 0 };
	page.on('request', (request) => {
		if (_isClientErrorPost(request)) tracker.sent += 1;
	});
	page.on('response', async (response) => {
		const request = response.request();
		if (!_isClientErrorPost(request)) return;
		try {
			const body = request.postDataJSON() as { message: string; context?: Record<string, unknown> };
			if (body.context?.source !== 'deck-load') return;
			const answer = (await response.json()) as { stored?: boolean };
			tracker.deckLoad.push({ message: body.message, context: body.context, stored: response.ok() && answer.stored === true });
		} finally {
			tracker.answered += 1;
		}
	});
	return tracker;
}

/**
 * Wait until reporting is QUIESCENT, so an exact count cannot pass on the first
 * report while a second is still on its way: the app's durable client-error
 * queue is empty (each row leaves it only after the server answered) and every
 * client-errors POST the page sent has been answered and recorded here. A
 * report is queued synchronously before the load promise rejects, so it cannot
 * slip in behind this check. A request that never answers fails the poll.
 */
async function _waitForReportQuiescence(page: import('@playwright/test').Page, tracker: ReportTracker): Promise<void> {
	await expect
		.poll(
			async () => {
				const queued = await page.evaluate(
					(key) => (JSON.parse(localStorage.getItem(key) ?? '[]') as unknown[]).length,
					CLIENT_ERROR_QUEUE_KEY
				);
				return queued === 0 && tracker.sent === tracker.answered;
			},
			{ message: 'client-error reporting never went quiet' }
		)
		.toBe(true);
}

/** Record every toast added from now on, so one that expires before the assertion still counts. */
async function _recordAddedToasts(page: import('@playwright/test').Page): Promise<void> {
	await page.evaluate(() => {
		const added: string[] = [];
		(window as unknown as { __toastsAdded: string[] }).__toastsAdded = added;
		new MutationObserver((records) => {
			for (const record of records) {
				for (const node of record.addedNodes) {
					if (node instanceof HTMLElement && node.matches('[data-toast-id]')) added.push(node.textContent ?? '');
				}
			}
		}).observe(document.body, { childList: true, subtree: true });
	});
}

function _readAddedToasts(page: import('@playwright/test').Page): Promise<string[]> {
	return page.evaluate(() => (window as unknown as { __toastsAdded: string[] }).__toastsAdded);
}

async function _openTrackifyFromGig(page: import('@playwright/test').Page): Promise<void> {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const picker = page.locator('details.mode-picker');
	await picker.locator('summary[aria-label="Choose app mode"]').click();
	await picker.locator('a.mode-card').filter({ hasText: 'Trackify' }).click();
	await page.waitForURL((url) => url.pathname === '/music-player');
	await expect(page.getByTestId('trackify-player')).toBeVisible();
	await page.waitForFunction(() => window.musicDjToolsTrackify?.version === 1);
}

test('mode chooser navigates to Trackify listening shell', async ({ page }) => {
	await _openTrackifyFromGig(page);
});

test('failed load skips to the next track with a dismissible toast within 2 s', async ({ page }) => {
	test.setTimeout(120_000);
	await _openTrackifyFromGig(page);
	const goodId = await page.evaluate(async () => {
		const response = await fetch('/api/v1/tracks?limit=1');
		if (!response.ok) throw new Error(`tracks list failed (${response.status})`);
		const payload = await response.json();
		const items = Array.isArray(payload.items) ? payload.items : [];
		if (items.length === 0) throw new Error('fixture library has no tracks');
		return items[0].stable_id as string;
	});
	const reports = _recordDeckLoadReports(page);
	await _recordAddedToasts(page);
	const startedAt = await page.evaluate(
		async ({ missingId, goodId }) => {
			const ipc = window.musicDjToolsTrackify;
			if (ipc?.e2e_prime_feed === undefined || ipc.e2e_force_load === undefined) {
				throw new Error('Trackify e2e hooks are unavailable');
			}
			ipc.toggle_autoplay(false);
			const perf = window.musicDjToolsPerformance;
			if (perf !== undefined && perf.query().decks[1].stable_id !== null) {
				await perf.dispatch({ type: 'unload', deck: 1 });
			}
			ipc.e2e_prime_feed([
				{
					stable_id: missingId,
					key: '8A',
					bpm: 124,
					file_exists: true,
					title: 'Missing',
					artist: 'E2E'
				},
				{
					stable_id: goodId,
					key: '8A',
					bpm: 124,
					file_exists: true,
					title: 'Good',
					artist: 'E2E'
				}
			]);
			const started = performance.now();
			await ipc.e2e_force_load(missingId);
			return started;
		},
		{ missingId: MISSING_STABLE_ID, goodId }
	);
	await expect(page.getByText(/Trackify: skipped track/)).toBeVisible({ timeout: 3_000 });
	const elapsedMs = await page.evaluate((started) => performance.now() - started, startedAt);
	expect(elapsedMs).toBeLessThan(2_000);
	await expect(page.locator('[data-toast-dismiss]').first()).toBeVisible();
	// #4036: the engine's own load-failure toast for Deck 1 used to
	// appear beside the skip toast. One failed load, one toast. Read from the
	// observer, not the live DOM: toHaveCount retries, and would pass once the
	// engine toast expired.
	const added = await _readAddedToasts(page);
	expect(added, `toasts added by one failed Trackify load: ${JSON.stringify(added)}`).toHaveLength(1);
	expect(added[0]).toMatch(/^Trackify: skipped track/);
	// The muted toast still owes the server its report: the only record of which
	// stage the load died in.
	await _waitForReportQuiescence(page, reports);
	expect(reports.deckLoad, 'one failed load, one deck-load report on the real backend').toHaveLength(1);
	expect(reports.deckLoad[0].stored, 'the real backend must have stored the report').toBe(true);
	expect(reports.deckLoad[0].context.deck).toBe(1);
	expect(Number.isFinite(reports.deckLoad[0].context.stage_failedAt), JSON.stringify(reports.deckLoad[0].context)).toBe(true);
	await page.waitForFunction(
		(goodId) => window.musicDjToolsTrackify?.query().deck.stable_id === goodId,
		goodId,
		{ timeout: 10_000 }
	);
	// Still one after the advance: nothing reports the same failure late.
	await _waitForReportQuiescence(page, reports);
	expect(reports.deckLoad, 'no second deck-load report arrived after the advance').toHaveLength(1);
});

test('control: an ordinary failed load keeps the engine toast and reports once', async ({ page }) => {
	const toastLog: string[] = [];
	page.on('console', (message) => {
		if (message.text().startsWith('[perf-event] toast-error')) toastLog.push(message.text());
	});
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const reports = _recordDeckLoadReports(page);
	await _recordAddedToasts(page);
	const rejected = await page.evaluate(async (missingId) => {
		try {
			await window.musicDjToolsPerformance?.dispatch({ type: 'load', deck: 1, stable_id: missingId });
			return false;
		} catch {
			return true;
		}
	}, MISSING_STABLE_ID);
	expect(rejected, 'a load of an unknown stable_id must reject').toBe(true);
	await _waitForReportQuiescence(page, reports);
	expect(reports.deckLoad, 'one failed load, one deck-load report on the real backend').toHaveLength(1);
	expect(reports.deckLoad[0].stored).toBe(true);
	const added = await _readAddedToasts(page);
	expect(
		added.filter((text) => text.includes('This track is no longer in the library.')),
		`if the engine toast is muted for every caller then a Gig load fails silently, and the engine and the dispatcher must not both toast it (pin a4898e22): ${JSON.stringify(added)}`
	).toHaveLength(1);
	expect(
		added.filter((text) => text.includes('Performance command failed')),
		`the dispatcher must not toast a failure the engine already put on screen: ${JSON.stringify(added)}`
	).toHaveLength(0);
	const deckToast = toastLog.find((line) => line.includes('Deck 1 load failed - '));
	expect(deckToast, JSON.stringify(toastLog)).toBeDefined();
	expect(deckToast, 'an RbApiError message already reads CODE: detail; the toast must not repeat the code').not.toContain(
		'TRACK_NOT_FOUND: TRACK_NOT_FOUND'
	);
});
