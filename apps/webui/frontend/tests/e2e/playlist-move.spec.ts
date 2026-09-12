/**
 * LIBM-22: multi-select grip drag issues one POST items:move, not PUT tracks.
 */
import { expect, test } from '@playwright/test';

const TRACK_ROW = '[data-testid="track-row"]';
const TREE = '[data-testid="playlist-tree"]';
const ROW = '[data-testid="playlist-row"]';

test('contiguous five-row grip drag POSTs items:move once', async ({ page }) => {
	const playlistName = `Move slice ${Date.now()}`;
	let moveCount = 0;
	let putTrackCount = 0;

	await page.goto('/performance');
	await page.waitForSelector(TRACK_ROW, { timeout: 60_000 });
	await page.waitForSelector(TREE, { timeout: 60_000 });

	const stableIds = await page.locator(TRACK_ROW).evaluateAll((rows) =>
		rows
			.slice(0, 6)
			.map((row) => row.getAttribute('data-stable-id'))
			.filter((id): id is string => id !== null && id !== '')
	);
	expect(stableIds.length).toBeGreaterThanOrEqual(6);

	const create = await page.request.post('/api/v1/playlists', {
		data: { name: playlistName }
	});
	expect(create.ok()).toBeTruthy();
	const playlistId = (await create.json()).playlist_id as string;

	for (const stableId of stableIds) {
		const add = await page.request.post(`/api/v1/playlists/${playlistId}/items:add`, {
			data: { stable_ids: [stableId] }
		});
		expect(add.ok()).toBeTruthy();
	}

	await page.locator(ROW).filter({ hasText: playlistName }).click();
	await page.waitForSelector(TRACK_ROW, { timeout: 30_000 });
	await expect(page.locator(TRACK_ROW)).toHaveCount(6);

	page.on('request', (request) => {
		const url = request.url();
		if (request.method() === 'POST' && url.includes('/items:move')) {
			moveCount += 1;
		}
		if (request.method() === 'PUT' && url.includes('/tracks')) {
			putTrackCount += 1;
		}
	});

	const rows = page.locator(TRACK_ROW);
	await rows.nth(0).click();
	await rows.nth(4).click({ modifiers: ['Shift'] });

	await page.evaluate(() => {
		const trackRows = [...document.querySelectorAll('[data-testid="track-row"]')];
		const sourceRow = trackRows[0];
		const targetRow = trackRows[5];
		if (!(sourceRow instanceof HTMLElement) || !(targetRow instanceof HTMLElement)) {
			throw new Error('expected six track rows');
		}
		const grip = sourceRow.querySelector('.grip');
		if (!(grip instanceof HTMLElement)) {
			throw new Error('expected grip on source row');
		}
		const dt = new DataTransfer();
		grip.dispatchEvent(
			new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: dt })
		);
		targetRow.dispatchEvent(
			new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt })
		);
		targetRow.dispatchEvent(
			new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt })
		);
	});

	await expect.poll(() => moveCount, { timeout: 10_000 }).toBe(1);
	expect(putTrackCount).toBe(0);

	const detail = await page.request.get(`/api/v1/playlists/${playlistId}`);
	const etag = detail.headers().etag;
	if (etag) {
		await page.request.delete(`/api/v1/playlists/${playlistId}`, {
			headers: { 'If-Match': etag }
		});
	}
});
