import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';

import { expect, test, type Page } from '@playwright/test';

import { FIXTURE_MANIFEST_PATH, POSIX_SERVERS } from './playwright.pairing-arrow.config';
import { waitForPerformanceIpc } from './support/performance-ready';

// requirement: PAIR-09 (the maintainer, Thu 8 Oct 2026: a paired track that is off screen
// gets a purple fold arrow that jumps to it)
// [if] the master deck's track has a paired partner scrolled out of the table
// [then] the PAIR fold arrow draws inside the table, names the partner's side,
// and a click brings the partner into view and hides the arrow [else stop].
//
// Codex P1 on #6062 asked for this to be exercised in a browser, because the
// stylesheet and unit tests cannot see whether the arrow actually renders.
// The library is the dedicated 26-track fixture (`--seed-pairing-arrow`); the
// root suite's 2-track library cannot put a partner off screen. The engine and
// /performance are real; the only writes are the real pairing API and the real
// deck commands.

interface ManifestTrack {
	stable_id: string;
	title: string;
}

interface PairingRow {
	pairing_id: string;
	from_stable_id: string;
	to_stable_id: string;
	direction: string;
	source: string;
	notes: string | null;
	snapshot: unknown;
	updated_at: string;
}

interface Init {
	method: string;
	body?: unknown;
	ifMatch?: string;
}

async function _api<T>(page: Page, path: string, init?: Init): Promise<T> {
	return page.evaluate(
		async ([url, opts]) => {
			const headers: Record<string, string> = {};
			if (opts?.body !== undefined) headers['content-type'] = 'application/json';
			if (opts?.ifMatch !== undefined) headers['if-match'] = opts.ifMatch;
			const res = await fetch(url, {
				method: opts?.method ?? 'GET',
				headers,
				body: opts?.body === undefined ? null : JSON.stringify(opts.body)
			});
			if (!res.ok) throw new Error(`${opts?.method ?? 'GET'} ${url} -> ${res.status}`);
			return res.status === 204 ? null : res.json();
		},
		[path, init] as const
	) as Promise<T>;
}

/** The server's strong validator for a pairing (apps/webui/server/etag.py). */
function _etag(row: PairingRow): string {
	return `"${createHash('sha1').update(`${row.pairing_id}:${row.updated_at}`).digest('hex')}"`;
}

async function _pairingsTouching(page: Page, ids: readonly string[]): Promise<PairingRow[]> {
	return (await _api<PairingRow[]>(page, '/api/v1/pairings')).filter(
		(row) => ids.includes(row.from_stable_id) || ids.includes(row.to_stable_id)
	);
}

/** The fixture rows must start unpaired. A test removes only the pairings it
 * created: deleting a pairing it did not create loses its id and timestamps,
 * which a re-POST cannot restore. A fixture that already pairs them fails here
 * and is left untouched. */
async function _requireUnpaired(page: Page, ids: readonly string[]): Promise<void> {
	expect(
		await _pairingsTouching(page, ids),
		'the fixture rows start unpaired; the test never removes a pairing it did not create'
	).toEqual([]);
}

async function _deletePairingsTouching(page: Page, ids: readonly string[]): Promise<void> {
	for (const row of await _pairingsTouching(page, ids)) {
		await _api(page, `/api/v1/pairings/${row.pairing_id}`, {
			method: 'DELETE',
			ifMatch: _etag(row)
		});
	}
}

async function _dispatch(page: Page, command: Record<string, unknown>): Promise<void> {
	await waitForPerformanceIpc(page);
	await page.evaluate(async (message) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch(message);
	}, command);
}

async function _masterDeck(page: Page): Promise<number | null> {
	await waitForPerformanceIpc(page);
	return page.evaluate(() => window.musicDjToolsPerformance?.query().master_deck ?? null);
}

test('a paired partner scrolled out of view draws the PAIR arrow, and a click jumps to it', async ({
	page
}) => {
	// Platform-specific: the server guard runs .venv/bin/python under /bin/sh, so
	// this spec runs on POSIX hosts only. Named here so the skip is never silent.
	test.skip(
		!POSIX_SERVERS,
		'platform-specific: the server guard runs .venv/bin/python under /bin/sh (POSIX only)'
	);
	const manifest = JSON.parse(readFileSync(FIXTURE_MANIFEST_PATH, 'utf8')) as {
		tracks: ManifestTrack[];
	};
	const master = manifest.tracks.find((t) => t.title === 'webkit-fixture-a-128bpm');
	expect(master, 'the fixture has the master deck track').toBeDefined();
	const browse = manifest.tracks.filter((t) => t.title.startsWith('webkit-fixture-arrow-'));
	expect(browse.length, 'the fixture has a browse list longer than one screen').toBeGreaterThan(20);
	const masterId = master!.stable_id;

	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await waitForPerformanceIpc(page);
	// The arrow is a relation to the MASTER deck's track, so load and master it.
	await _dispatch(page, { type: 'load', deck: 1, stable_id: masterId });
	await _dispatch(page, { type: 'master', deck: 1 });
	await expect.poll(() => _masterDeck(page)).toBe(1);

	// A partner the table does not render right now is off screen by definition.
	const rendered = await page
		.locator('[data-testid="track-row"]')
		.evaluateAll((els) => els.map((el) => el.getAttribute('data-stable-id')));
	const partner = browse.find((t) => !rendered.includes(t.stable_id));
	expect(partner, 'at least one browse track is scrolled out of view').toBeDefined();
	const partnerId = partner!.stable_id;
	const partnerRow = page.locator(`[data-testid="track-row"][data-stable-id="${partnerId}"]`);

	// The fixture must start unpaired, and the finally below removes only the
	// pairing this test creates, so a run that fails partway leaves the fixture as it was.
	const ids = [masterId, partnerId];
	await _requireUnpaired(page, ids);
	try {
		await _api(page, '/api/v1/pairings', {
			method: 'POST',
			body: {
				from_stable_id: masterId,
				to_stable_id: partnerId,
				direction: '<->',
				source: 'manual',
				notes: null,
				snapshot: null
			}
		});
		// The pairing went in through the API, not this page's own write, so this
		// page does not hear about it. A reload is the path that loads the graph.
		await page.reload({ waitUntil: 'domcontentloaded' });
		await waitForPerformanceIpc(page);
		// The reload drops the deck load and the page-local master election, and the
		// PAIR arrow reads its partners only from the elected master, so load and
		// elect deck 1 again.
		await _dispatch(page, { type: 'load', deck: 1, stable_id: masterId });
		await _dispatch(page, { type: 'master', deck: 1 });
		await expect.poll(() => _masterDeck(page)).toBe(1);
		await expect(partnerRow, 'the partner is not rendered, so it is off screen').toHaveCount(0);

		const fold = page.getByTestId('pair-fold');
		await expect(fold, 'the arrow draws for an off-screen partner').toBeVisible();
		await expect(fold).toHaveAttribute('title', /Paired track is (above|below)/);
		const box = await fold.boundingBox();
		expect(box, 'the arrow has a box').not.toBeNull();
		expect(box!.width, 'the arrow has a painted width').toBeGreaterThan(0);
		expect(box!.height, 'the arrow has a painted height').toBeGreaterThan(0);
		const viewport = page.viewportSize();
		expect(box!.x, 'the arrow sits inside the page').toBeGreaterThanOrEqual(0);
		expect(box!.x + box!.width, 'the arrow sits inside the page').toBeLessThanOrEqual(
			viewport!.width
		);

		// The arrow is a button: clicking it must bring the partner into view.
		await fold.click();
		await expect(partnerRow, 'the click jumps the table to the partner').toBeVisible();
		await expect(fold, 'the arrow hides while its partner is on screen').toHaveCount(0);
	} finally {
		await _deletePairingsTouching(page, ids);
	}
	expect(await _pairingsTouching(page, ids), 'the test leaves no pairing behind').toEqual([]);
});
