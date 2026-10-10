/**
 * requirement: GRIDFLAG-04
 *
 * The "Dismiss beatgrid flag" button in the library table's analysis-dots
 * popover (Err column) is connected: a click sends
 * `PUT /api/v1/beatgrid-flags/{stable_id}/dismissed` to the real engine, the
 * engine stores it, and the button turns into "Restore beatgrid flag", which
 * sends the opposite write.
 *
 * Before this spec the only guard was a regex over TrackTable.svelte's source
 * (`grid-flag-row.test.mjs`), which proves the handler text is in the file and
 * never that a click reaches the engine.
 *
 * WHAT IS REAL AND WHAT IS NOT. The root fixture library has no rekordbox
 * beatgrid, so every row's stored verdict is `unknown` and the popover offers
 * no dismiss control. No HTTP route can set a verdict (only a scan of a real
 * grid can), so this spec rewrites ONE field group on ONE row of the
 * `GET /tracks/index` response on its way to the page: `grid_class`, `message`
 * and `reason`. The row's `dismissed` bit is passed through untouched, so the
 * Restore state the page shows after the click comes from what the engine
 * stored. The click, the PUT, its 200 and the stored bit are all the real
 * engine, and the stored bit is read back through an unpatched request.
 *
 * WHAT A PASS HERE DOES NOT COVER: a real `suspect` verdict reaching the row
 * ((private path) covers that end), and the packaged app.
 *
 * Regression lines:
 * - if TrackTable stops passing `ongridflagdismiss` then the button is inert
 *   with the planned-explainer tooltip: broken
 * - if a click sends no PUT, or sends it for another track, then broken
 * - if the engine does not store the dismissal then broken
 * - if a dismissed flag cannot be restored from the same popover then broken
 * - control: if the fixture row is really flagged then the patch is not what
 *   lit the control, and this spec is measuring something else
 */
import { expect, test, type Locator, type Page, type Request } from '@playwright/test';

const INDEX_PATH = '/api/v1/tracks/index';
const SUSPECT_MESSAGE =
	'Beatgrid: 223 of 616 beat intervals are uneven (worst 30 ms off at 137.5 s), but 96% of beats sit on ' +
	'one 128.00 BPM line (the grid is uneven, the music is not) - Beat Sync may wander on this track';
const INERT_TITLE_FRAGMENT = 'no dismiss handler connected';

interface GridQualityWire {
	grid_class: string;
	dismissed: boolean;
	message: string | null;
	reason: string | null;
}

interface IndexRowWire {
	stable_id: string;
	grid_quality: GridQualityWire | null;
}

/** The engine's own, unpatched row (`page.request` bypasses `page.route`). */
async function storedGridQuality(page: Page, stableId: string): Promise<GridQualityWire> {
	const response = await page.request.get(INDEX_PATH);
	expect(response.status()).toBe(200);
	const index = (await response.json()) as { items: IndexRowWire[] };
	const row = index.items.find((item) => item.stable_id === stableId);
	if (row === undefined || row.grid_quality === null) {
		throw new Error(`track ${stableId} has no grid_quality block in ${INDEX_PATH}`);
	}
	return row.grid_quality;
}

/** Show `stableId` to the page as a flagged grid; every other field, and every
 * other row, is the engine's. Conditional headers are dropped so the engine
 * always answers with a body to rewrite. */
async function presentRowAsSuspect(page: Page, stableId: string): Promise<void> {
	await page.route(`**${INDEX_PATH}`, async (route) => {
		const headers = { ...route.request().headers() };
		delete headers['if-none-match'];
		delete headers['if-modified-since'];
		const response = await route.fetch({ headers });
		if (response.status() !== 200) throw new Error(`${INDEX_PATH} answered ${response.status()}`);
		const index = (await response.json()) as { items: IndexRowWire[] };
		const row = index.items.find((item) => item.stable_id === stableId);
		if (row === undefined || row.grid_quality === null) {
			throw new Error(`track ${stableId} is missing from ${INDEX_PATH}`);
		}
		row.grid_quality = {
			...row.grid_quality,
			grid_class: 'suspect',
			message: SUSPECT_MESSAGE,
			reason: null
		};
		await route.fulfill({ response, json: index });
	});
}

async function openIssuesPopover(page: Page, stableId: string): Promise<Locator> {
	const trigger = page.locator(
		`[data-testid="track-row"][data-stable-id="${stableId}"] td.c-err [aria-label="Data-quality issues"]`
	);
	await page.mouse.move(0, 0);
	await trigger.hover();
	const popover = trigger.getByTestId('analysis-dots-pop');
	await expect(popover).toBeVisible();
	return popover.getByTestId('grid-flag-dismiss');
}

async function clickAndCaptureWrite(page: Page, button: Locator, stableId: string): Promise<Request> {
	const [request] = await Promise.all([
		page.waitForRequest(
			(candidate) =>
				candidate.method() === 'PUT' &&
				new URL(candidate.url()).pathname === `/api/v1/beatgrid-flags/${stableId}/dismissed`
		),
		button.click()
	]);
	return request;
}

async function expectStoredWrite(request: Request, stableId: string, dismissed: boolean): Promise<void> {
	expect(request.postDataJSON()).toEqual({ dismissed });
	const response = await request.response();
	if (response === null) throw new Error('the dismiss PUT got no response');
	expect(response.status()).toBe(200);
	const body = (await response.json()) as { stable_id: string; dismissed: boolean; etag: string };
	expect(body.stable_id).toBe(stableId);
	expect(body.dismissed).toBe(dismissed);
	expect(body.etag).not.toBe('');
}

test('Dismiss beatgrid flag in the Err popover writes to the engine, and Restore undoes it', async ({ page }) => {
	test.setTimeout(90_000);

	const seed = await page.request.get(INDEX_PATH);
	expect(seed.status()).toBe(200);
	const seedIndex = (await seed.json()) as { items: IndexRowWire[] };
	expect(seedIndex.items.length).toBeGreaterThan(0);
	const stableId = seedIndex.items[0]!.stable_id;

	// Control: the fixture row is NOT flagged and NOT dismissed in the engine.
	const before = await storedGridQuality(page, stableId);
	expect(before.grid_class).toBe('unknown');
	expect(before.dismissed).toBe(false);

	await presentRowAsSuspect(page, stableId);
	await page.goto('/performance');
	await page.getByText('All Tracks', { exact: true }).first().click();
	await expect(page.locator(`[data-testid="track-row"][data-stable-id="${stableId}"]`)).toBeVisible({
		timeout: 30_000
	});

	const dismiss = await openIssuesPopover(page, stableId);
	await expect(dismiss).toHaveText('Dismiss beatgrid flag');
	await expect(dismiss).toBeEnabled();
	expect(await dismiss.getAttribute('title')).not.toContain(INERT_TITLE_FRAGMENT);

	const dismissWrite = await clickAndCaptureWrite(page, dismiss, stableId);
	await expectStoredWrite(dismissWrite, stableId, true);
	await expect.poll(async () => (await storedGridQuality(page, stableId)).dismissed).toBe(true);

	const restore = await openIssuesPopover(page, stableId);
	await expect(restore).toHaveText('Restore beatgrid flag');
	await expect(restore).toBeEnabled();

	const restoreWrite = await clickAndCaptureWrite(page, restore, stableId);
	await expectStoredWrite(restoreWrite, stableId, false);
	await expect.poll(async () => (await storedGridQuality(page, stableId)).dismissed).toBe(false);

	const again = await openIssuesPopover(page, stableId);
	await expect(again).toHaveText('Dismiss beatgrid flag');
});
