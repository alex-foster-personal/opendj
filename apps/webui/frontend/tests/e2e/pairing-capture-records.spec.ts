import { createHash } from 'node:crypto';

import { expect, test, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

// requirement: PAIR-07 (orders board W19; the maintainer, Tue 6 Oct 2026: "Pairings
// 'capture' - this should work")
// [if] two decks are loaded and both are checked in Create pairing [then]
// Capture creates the pairing (GET /api/v1/pairings) AND records a sync
// snapshot (GET /api/v1/pairings/sync-snapshots) at the frozen positions
// [else stop].
// [if] the same pair is captured again at new positions [then] a second sync
// snapshot is recorded at those positions, so the recapture is not a silent
// no-op [else stop]. Before W19 the pairing row is write-once (PAIR-04) and
// Capture wrote nothing else, so a recapture changed nothing on the server
// while the toast said "Pairing captured", and Reload sync never had a
// snapshot to replay.
// [if] the test ends, pass or fail [then] the pairings it created are deleted,
// so later specs in the same run (pairing-library-edge needs two unpaired
// rows in this small fixture) see the library as seeded [else stop: the run's
// later pairing specs fail on a fixture this spec paired].

const SHEET = '[role=dialog][aria-label="Create pairing"]';

interface SyncSnapshotRow {
	stable_a: string;
	stable_b: string;
	a_position_ms: number;
	b_position_ms: number;
}

async function _api<T>(page: Page, path: string): Promise<T> {
	return page.evaluate(async (url) => {
		const res = await fetch(url);
		if (!res.ok) throw new Error(`${url} -> ${res.status}`);
		return res.json();
	}, path);
}

interface PairingRow {
	pairing_id: string;
	from_stable_id: string;
	to_stable_id: string;
	updated_at: string;
}

/** The server's strong validator for a pairing (apps/webui/server/etag.py). */
function _etag(row: PairingRow): string {
	return `"${createHash('sha1').update(`${row.pairing_id}:${row.updated_at}`).digest('hex')}"`;
}

/** Delete the pairings that exist now but did not before the test. */
async function _deletePairingsCreatedSince(page: Page, before: ReadonlySet<string>): Promise<void> {
	for (const row of await _api<PairingRow[]>(page, '/api/v1/pairings')) {
		if (before.has(row.pairing_id)) continue;
		await page.evaluate(
			async ([url, etag]) => {
				const res = await fetch(url, { method: 'DELETE', headers: { 'if-match': etag } });
				if (!res.ok) throw new Error(`DELETE ${url} -> ${res.status}`);
			},
			[`/api/v1/pairings/${row.pairing_id}`, _etag(row)] as const
		);
	}
}

async function _capture(page: Page, positions: [number, number]): Promise<void> {
	for (const [index, position_ms] of positions.entries()) {
		await page.evaluate(
			([deck, ms]) =>
				window.musicDjToolsPerformance!.dispatch({ type: 'seek', deck, position_ms: ms, exact: true }),
			[index + 1, position_ms] as const
		);
	}
	const pairing = page.locator('button.topbar-slot-pairing');
	await expect(pairing).toBeEnabled();
	await pairing.click();
	const sheet = page.locator(SHEET);
	await expect(sheet).toBeVisible();
	await expect(sheet.locator('input[type=checkbox]:checked')).toHaveCount(2);
	await sheet.getByRole('button', { name: 'Capture', exact: true }).click();
	// The sheet closes only on success (PAIR-06), after the durable writes land.
	await expect(page.getByText('Pairing captured', { exact: true })).toBeVisible();
	await expect(sheet).toBeHidden();
}

function _isPairingCapturePost(url: string, method: string): boolean {
	if (method !== 'POST') return false;
	const path = new URL(url, 'http://fixture').pathname;
	return path === '/api/v1/pairings' || path === '/api/v1/pairings/sync-snapshots';
}

test('Capture with 2 checked decks creates the pairing and records a sync snapshot each time', async ({
	page
}) => {
	await page.setViewportSize({ width: 1600, height: 900 });
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await waitForPerformanceIpc(page);

	const tracks = await _api<{ items: Array<{ stable_id: string }> }>(page, '/api/v1/tracks?limit=2');
	expect(tracks.items.length, 'the fixture library has two tracks').toBeGreaterThanOrEqual(2);
	const [a, b] = tracks.items.map((track) => track.stable_id);
	for (const [index, stable_id] of [a, b].entries()) {
		await page.evaluate(
			([deck, sid]) => window.musicDjToolsPerformance!.dispatch({ type: 'load', deck, stable_id: sid }),
			[index + 1, stable_id] as const
		);
	}
	// BeatSyncMax (on by default) records beats, so Capture stays disabled with
	// it on (PAIR-05). One library track has an own beatgrid for BPM-provenance
	// e2e; this test clicks BSM off before capture regardless.
	const bsm = page.locator('button.topbar-slot-bsm');
	if ((await bsm.getAttribute('aria-pressed')) === 'true') await bsm.click();
	await expect(bsm).toHaveAttribute('aria-pressed', 'false');

	const before = new Set((await _api<PairingRow[]>(page, '/api/v1/pairings')).map((row) => row.pairing_id));
	const pairingPosts: string[] = [];
	const onRequest = (request: { url: () => string; method: () => string }) => {
		if (_isPairingCapturePost(request.url(), request.method())) {
			pairingPosts.push(request.url());
		}
	};
	page.on('request', onRequest);
	try {
		const snapshots = (): Promise<SyncSnapshotRow[]> =>
			_api<SyncSnapshotRow[]>(page, `/api/v1/pairings/sync-snapshots?stable_a=${a}&stable_b=${b}&limit=50`);
		expect(await snapshots()).toHaveLength(0);

		await _capture(page, [30_000, 45_000]);
		expect(pairingPosts, 'successful Capture POSTs pairing + sync-snapshot').toHaveLength(2);
		const pairings = await _api<Array<{ from_stable_id: string; to_stable_id: string }>>(
			page,
			`/api/v1/pairings?from_stable_id=${a}`
		);
		expect(pairings.some((row) => row.to_stable_id === b), 'Capture created the pairing').toBe(true);
		await expect.poll(async () => (await snapshots()).length).toBe(1);
		const first = (await snapshots())[0];
		expect(first.a_position_ms).toBeCloseTo(30_000, -2);
		expect(first.b_position_ms).toBeCloseTo(45_000, -2);

		// A recapture of the now-saved pair is not a no-op.
		await _capture(page, [10_000, 20_000]);
		await expect.poll(async () => (await snapshots()).length).toBe(2);
		const positions = (await snapshots()).map((row) => Math.round(row.a_position_ms / 1000));
		expect(positions.sort((x, y) => x - y)).toEqual([10, 30]);
	} finally {
		page.off('request', onRequest);
		await _deletePairingsCreatedSince(page, before);
	}
});

// requirement: PAIR-12881 (issue #12384)
// [if] a deck is unloaded while Create pairing is open [then] Capture shows
// "Both decks must be loaded" and posts nothing [else stop].
test('Capture after unload refuses with named toast and no pairing POSTs', async ({ page }) => {
	await page.setViewportSize({ width: 1600, height: 900 });
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await waitForPerformanceIpc(page);

	const tracks = await _api<{ items: Array<{ stable_id: string }> }>(page, '/api/v1/tracks?limit=2');
	expect(tracks.items.length, 'the fixture library has two tracks').toBeGreaterThanOrEqual(2);
	const [a, b] = tracks.items.map((track) => track.stable_id);
	for (const [index, stable_id] of [a, b].entries()) {
		await page.evaluate(
			([deck, sid]) => window.musicDjToolsPerformance!.dispatch({ type: 'load', deck, stable_id: sid }),
			[index + 1, stable_id] as const
		);
	}
	const bsm = page.locator('button.topbar-slot-bsm');
	if ((await bsm.getAttribute('aria-pressed')) === 'true') await bsm.click();
	await expect(bsm).toHaveAttribute('aria-pressed', 'false');

	const before = new Set((await _api<PairingRow[]>(page, '/api/v1/pairings')).map((row) => row.pairing_id));
	const pairingPosts: string[] = [];
	const onRequest = (request: { url: () => string; method: () => string }) => {
		if (_isPairingCapturePost(request.url(), request.method())) {
			pairingPosts.push(request.url());
		}
	};
	page.on('request', onRequest);

	try {
		const pairing = page.locator('button.topbar-slot-pairing');
		await expect(pairing).toBeEnabled();
		await pairing.click();
		const sheet = page.locator(SHEET);
		await expect(sheet).toBeVisible();
		await expect(sheet.locator('input[type=checkbox]:checked')).toHaveCount(2);

		const unloadDeck = await page.evaluate(() => {
			const decks = window.musicDjToolsPerformance!.query().decks;
			const d1 = decks[1];
			const d2 = decks[2];
			if (d1 === undefined || d2 === undefined) throw new Error('missing decks');
			if (d1.is_master) return 2 as const;
			if (d2.is_master) return 1 as const;
			return 2 as const;
		});
		await page.evaluate(
			(deck) => window.musicDjToolsPerformance!.dispatch({ type: 'unload', deck }),
			unloadDeck
		);

		await sheet.getByRole('button', { name: 'Capture', exact: true }).click();
		await expect(sheet).toBeVisible();
		await expect(page.locator('span.toast-headline', { hasText: 'Both decks must be loaded' })).toBeVisible();
		expect(pairingPosts, 'no durable pairing or sync-snapshot POSTs').toHaveLength(0);
		const after = await _api<PairingRow[]>(page, '/api/v1/pairings');
		expect(new Set(after.map((row) => row.pairing_id))).toEqual(before);
	} finally {
		page.off('request', onRequest);
		await _deletePairingsCreatedSince(page, before);
	}
});
