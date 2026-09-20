import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test, expect } from '@playwright/test';

const FIXTURE_MP3 = path.resolve(
	fileURLToPath(new URL('.', import.meta.url)),
	'../../../../../tests/fixtures/phase7-dedup/src-128.mp3'
);

test.describe('playlist folder drop', () => {
	test('file drag over playlist tree does not open generic ingest overlay', async ({ page }) => {
		await page.goto('/performance');
		await expect(page.getByTestId('refresh-analysis')).toBeVisible({ timeout: 15_000 });
		await expect(page.getByTestId('playlist-tree-panel')).toBeVisible();

		await page.evaluate(() => {
			const panel = document.querySelector('[data-testid="playlist-tree-panel"]');
			const dt = new DataTransfer();
			dt.items.add(new File([new Uint8Array(8)], 'junk.txt', { type: 'text/plain' }));
			panel?.dispatchEvent(
				new DragEvent('dragenter', { dataTransfer: dt, bubbles: true, cancelable: true })
			);
			panel?.dispatchEvent(
				new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true })
			);
		});

		await expect(page.getByTestId('ingest-drop-overlay')).not.toBeVisible();
		await expect(page.getByTestId('ingest-modal')).not.toBeVisible();
	});

	test('file drag elsewhere still opens ingest overlay', async ({ page }) => {
		await page.goto('/performance');
		await expect(page.getByTestId('refresh-analysis')).toBeVisible({ timeout: 15_000 });

		await page.evaluate(() => {
			const dt = new DataTransfer();
			dt.items.add(new File([new Uint8Array(4096).fill(65)], 'e2e-junk.mp3', { type: 'audio/mpeg' }));
			window.dispatchEvent(new DragEvent('dragenter', { dataTransfer: dt, bubbles: true }));
		});

		await expect(page.getByTestId('ingest-drop-overlay')).toBeVisible();
	});

	test('materialize plus items:add adds staged tracks to a playlist', async ({ page }) => {
		test.skip(!fs.existsSync(FIXTURE_MP3), 'phase7 dedup fixture mp3 not present');

		const batch = 'e2e-folder-drop';
		const upload = await page.request.post('/api/v1/ingest/upload', {
			multipart: {
				batch,
				force: 'false',
				files: {
					name: 'files',
					mimeType: 'audio/mpeg',
					buffer: fs.readFileSync(FIXTURE_MP3)
				}
			}
		});
		expect(upload.ok()).toBe(true);

		const mat = await page.request.post(`/api/v1/ingest/batch/${batch}/materialize`);
		expect(mat.ok()).toBe(true);
		const matBody = (await mat.json()) as { tracks: { stable_id: string }[] };
		expect(matBody.tracks.length).toBeGreaterThan(0);

		const created = await page.request.post('/api/v1/playlists', { data: { name: 'E2E Folder Drop' } });
		expect(created.ok()).toBe(true);
		const playlistId = (await created.json()).playlist_id as string;

		const stableIds = matBody.tracks.map((t) => t.stable_id);
		const add = await page.request.post(`/api/v1/playlists/${playlistId}/items:add`, {
			data: { stable_ids: stableIds }
		});
		expect(add.ok()).toBe(true);

		const detail = await page.request.get(`/api/v1/playlists/${playlistId}`);
		expect(detail.ok()).toBe(true);
		const tracks = ((await detail.json()) as { tracks: unknown[] }).tracks;
		expect(tracks.length).toBeGreaterThanOrEqual(stableIds.length);
	});
});
