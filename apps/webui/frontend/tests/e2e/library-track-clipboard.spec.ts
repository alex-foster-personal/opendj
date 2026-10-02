/**
 * LIBM-160..162 (pins ce142ae7e22f, 0b1e12cc01d0): Cmd/Ctrl+A selects every
 * row, Cmd/Ctrl+C copies, and Cmd/Ctrl+V pastes into the open playlist, which
 * shows the pasted rows at once without being reopened.
 */
import { expect, test } from '@playwright/test';

const TRACK_ROW = '[data-testid="track-row"]';
const TREE = '[data-testid="playlist-tree"]';
const ROW = '[data-testid="playlist-row"]';

/** Playlists the running test created; afterEach deletes them. */
let created: string[] = [];

async function createPlaylist(
	page: import('@playwright/test').Page,
	name: string,
	stableIds: string[]
): Promise<string> {
	const create = await page.request.post('/api/v1/playlists', { data: { name } });
	expect(create.ok()).toBeTruthy();
	const id = (await create.json()).playlist_id as string;
	created.push(id);
	if (stableIds.length > 0) {
		const add = await page.request.post(`/api/v1/playlists/${id}/items:add`, {
			data: { stable_ids: stableIds }
		});
		expect(add.ok()).toBeTruthy();
	}
	return id;
}

/** Delete what a test created, so later specs (track-playlists' "Show in
 * playlists" popover lists every playlist holding a track) see the fixture
 * library as it was. Same etag dance as track-playlists.spec.ts. */
async function deletePlaylists(
	page: import('@playwright/test').Page,
	ids: readonly string[]
): Promise<void> {
	for (const id of ids) {
		const detail = await page.request.get(`/api/v1/playlists/${id}`);
		if (!detail.ok()) continue;
		const etag = detail.headers().etag;
		if (!etag) continue;
		await page.request.delete(`/api/v1/playlists/${id}`, { headers: { 'If-Match': etag } });
	}
}

async function memberIds(page: import('@playwright/test').Page, id: string): Promise<string[]> {
	const r = await page.request.get(`/api/v1/playlists/${id}`);
	expect(r.ok()).toBeTruthy();
	return ((await r.json()).tracks as { stable_id: string }[]).map((t) => t.stable_id);
}

test.afterEach(async ({ page }) => {
	const ids = created;
	created = [];
	await deletePlaylists(page, ids);
});

test('Cmd+A, Cmd+C, Cmd+V copies tracks into another playlist and shows them at once', async ({
	page
}) => {
	const stamp = Date.now();
	await page.goto('/performance');
	await page.waitForSelector(TRACK_ROW, { timeout: 60_000 });
	await page.waitForSelector(TREE, { timeout: 60_000 });

	const libraryIds = await page.locator(TRACK_ROW).evaluateAll((rows) =>
		rows
			.map((row) => row.getAttribute('data-stable-id'))
			.filter((id): id is string => id !== null && id !== '')
	);
	const distinct = [...new Set(libraryIds)];
	expect(distinct.length).toBeGreaterThan(0);

	const sourceName = `Clip source ${stamp}`;
	const destName = `Clip dest ${stamp}`;
	await createPlaylist(page, sourceName, distinct);
	const destId = await createPlaylist(page, destName, []);
	// The tree learns about playlists created over HTTP from the events bus.
	await expect(page.locator(ROW).filter({ hasText: destName })).toBeVisible({ timeout: 15_000 });

	await page.locator(ROW).filter({ hasText: sourceName }).click();
	await expect(page.locator(TRACK_ROW)).toHaveCount(distinct.length, { timeout: 30_000 });

	await page.locator(TRACK_ROW).first().click();
	await page.keyboard.press('ControlOrMeta+a');
	await expect(page.locator(`${TRACK_ROW}.rb-row-selected`)).toHaveCount(distinct.length);
	await page.keyboard.press('ControlOrMeta+c');
	await expect(page.getByText(/^Copied \d+ tracks?/)).toBeVisible({ timeout: 10_000 });

	await page.locator(ROW).filter({ hasText: destName }).click();
	await expect(page.locator(TRACK_ROW)).toHaveCount(0, { timeout: 30_000 });
	// Focus stays on the tree row just clicked, which is not a text field.
	await page.keyboard.press('ControlOrMeta+v');
	await expect(page.getByText(new RegExp(`Pasted \\d+ tracks? into "${destName}"`))).toBeVisible({
		timeout: 10_000
	});

	// The open playlist shows the pasted rows without being reopened.
	await expect(page.locator(TRACK_ROW)).toHaveCount(distinct.length, { timeout: 10_000 });
	expect((await memberIds(page, destId)).sort()).toEqual([...distinct].sort());

	// A second paste adds nothing and says so.
	await page.keyboard.press('ControlOrMeta+v');
	await expect(page.getByText(/Nothing new to paste/)).toBeVisible({ timeout: 10_000 });
	expect(await memberIds(page, destId)).toHaveLength(distinct.length);
});

test('a paste into a long playlist selects the appended rows and scrolls them into view', async ({
	page
}) => {
	const stamp = Date.now();
	await page.goto('/performance');
	await page.waitForSelector(TRACK_ROW, { timeout: 60_000 });
	await page.waitForSelector(TREE, { timeout: 60_000 });

	const libraryIds = await page.locator(TRACK_ROW).evaluateAll((rows) =>
		rows
			.map((row) => row.getAttribute('data-stable-id'))
			.filter((id): id is string => id !== null && id !== '')
	);
	const distinct = [...new Set(libraryIds)];
	// Two tracks are enough: the destination repeats one 80 times (memberships,
	// not distinct tracks) and the paste brings the other, which lands at 81.
	expect(distinct.length).toBeGreaterThan(1);
	const [filler, pasted] = distinct;

	const sourceName = `Clip one ${stamp}`;
	const destName = `Clip long ${stamp}`;
	await createPlaylist(page, sourceName, [pasted]);
	await createPlaylist(page, destName, Array.from({ length: 80 }, () => filler));
	await expect(page.locator(ROW).filter({ hasText: destName })).toBeVisible({ timeout: 15_000 });

	await page.locator(ROW).filter({ hasText: sourceName }).click();
	await expect(page.locator(TRACK_ROW)).toHaveCount(1, { timeout: 30_000 });
	await page.locator(TRACK_ROW).first().click();
	await page.keyboard.press('ControlOrMeta+c');
	await expect(page.getByText(/^Copied 1 track/)).toBeVisible({ timeout: 10_000 });

	await page.locator(ROW).filter({ hasText: destName }).click();
	await expect(page.locator(`${TRACK_ROW}[data-stable-id="${filler}"]`).first()).toBeVisible({
		timeout: 30_000
	});
	await page.keyboard.press('ControlOrMeta+v');
	await expect(page.getByText(new RegExp(`Pasted 1 track into "${destName}"`))).toBeVisible({
		timeout: 10_000
	});

	const landed = page.locator(`${TRACK_ROW}[data-stable-id="${pasted}"]`);
	await expect(landed).toBeVisible({ timeout: 10_000 });
	await expect(landed).toHaveClass(/rb-row-selected/);
	// The pasted row sits inside the track table's visible band. The table can
	// itself be partly below the page fold at this viewport, so compare the row
	// with its scroll container rather than with the window.
	const band = await landed.evaluate((row) => {
		const wrap = row.closest('.table-wrap');
		if (wrap === null) return null;
		const r = row.getBoundingClientRect();
		const w = wrap.getBoundingClientRect();
		return { rowTop: r.top, rowBottom: r.bottom, wrapTop: w.top, wrapBottom: w.bottom };
	});
	expect(band).not.toBeNull();
	expect(band!.rowTop).toBeGreaterThanOrEqual(band!.wrapTop);
	expect(band!.rowBottom).toBeLessThanOrEqual(band!.wrapBottom);
});
