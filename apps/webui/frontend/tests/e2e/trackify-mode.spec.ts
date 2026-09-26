// requirement: PERFMODE-15
// [if] Trackify route is opened from the mode chooser [then] the listening shell renders
// [if] a Trackify load fails [then] exactly one toast shows, the skip toast (#4036)
//   [⛔️ if the engine's "Deck 1 could not load the track" toast shows as well, or none]
// [if] that engine toast is muted for Trackify [then] the real backend still stores
//   exactly one deck-load report carrying deck and stage_failedAt
//   [⛔️ if muting the toast drops the report]
// [if] an ordinary (non-Trackify) load fails [then] the engine toast still shows and
//   reports once, and its message names the error code once
//   [⛔️ if the fix muted the engine toast for every caller]
// Everything here runs against the real engine daemon and fixture library: the
// 404 is the production track route's, and each report is read back from the
// production /api/v1/client-errors response, never fabricated.

import { expect, test } from '@playwright/test';

const MISSING_STABLE_ID = 'trackify-e2e-missing-stable-id';

type DeckLoadReport = { message: string; context: Record<string, unknown>; stored: boolean };

/** Every deck-load client-error report the page sends, with the real backend's answer. */
function _recordDeckLoadReports(page: import('@playwright/test').Page): DeckLoadReport[] {
	const reports: DeckLoadReport[] = [];
	page.on('response', async (response) => {
		const request = response.request();
		if (request.method() !== 'POST' || !request.url().includes('/api/v1/client-errors')) return;
		const body = request.postDataJSON() as { message: string; context?: Record<string, unknown> };
		if (body.context?.source !== 'deck-load') return;
		const answer = (await response.json()) as { stored?: boolean };
		reports.push({ message: body.message, context: body.context, stored: response.ok() && answer.stored === true });
	});
	return reports;
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
	// #4036: the engine's own "Deck 1 could not load the track" toast used to
	// appear beside the skip toast. One failed load, one toast. Read from the
	// observer, not the live DOM: toHaveCount retries, and would pass once the
	// engine toast expired.
	const added = await _readAddedToasts(page);
	expect(added, `toasts added by one failed Trackify load: ${JSON.stringify(added)}`).toHaveLength(1);
	expect(added[0]).toMatch(/^Trackify: skipped track/);
	// The muted toast still owes the server its report: the only record of which
	// stage the load died in.
	await expect.poll(() => reports.length, { message: 'the real backend never received the deck-load report' }).toBe(1);
	expect(reports[0].stored, 'the real backend must have stored the report').toBe(true);
	expect(reports[0].context.deck).toBe(1);
	expect(Number.isFinite(reports[0].context.stage_failedAt), JSON.stringify(reports[0].context)).toBe(true);
	await page.waitForFunction(
		(goodId) => window.musicDjToolsTrackify?.query().deck.stable_id === goodId,
		goodId,
		{ timeout: 10_000 }
	);
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
	await expect.poll(() => reports.length, { message: 'the real backend never received the deck-load report' }).toBe(1);
	expect(reports[0].stored).toBe(true);
	const added = await _readAddedToasts(page);
	expect(
		added.some((text) => text.includes('Deck 1 could not load the track')),
		`if the engine toast is muted for every caller then a Gig load fails silently: ${JSON.stringify(added)}`
	).toBe(true);
	const deckToast = toastLog.find((line) => line.includes('Deck 1 load failed - '));
	expect(deckToast, JSON.stringify(toastLog)).toBeDefined();
	expect(deckToast, 'an RbApiError message already reads CODE: detail; the toast must not repeat the code').not.toContain(
		'TRACK_NOT_FOUND: TRACK_NOT_FOUND'
	);
});
