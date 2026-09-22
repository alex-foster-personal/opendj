/**
 * @vocals-demucs-overlay
 *
 * Exercises the hydrated listing demucs vocal overlay: API row carries
 * ``vocals.status === 'demucs'`` and PreviewStrip paints VOCAL_BLUE pixels.
 */
import { expect, test } from '@playwright/test';
import { existsSync, readFileSync } from 'node:fs';

import { FIXTURE_MANIFEST } from './playwright.vocals-demucs-overlay.config';

const VOCAL_BLUE = { r: 0x4f, g: 0xb2, b: 0xff };
const TRACK_ROW = '[data-testid="track-row"]';

interface FixtureManifest {
	stable_id?: string;
	playlist_id?: string;
	demucs_ready?: boolean;
}

function _readManifest(): FixtureManifest {
	if (!existsSync(FIXTURE_MANIFEST)) {
		throw new Error(`UNKNOWN: fixture manifest missing at ${FIXTURE_MANIFEST}`);
	}
	return JSON.parse(readFileSync(FIXTURE_MANIFEST, 'utf8')) as FixtureManifest;
}

test('@vocals-demucs-overlay hydrated listing paints demucs vocal pixels', async ({
	page,
	request
}) => {
	const manifest = _readManifest();
	expect(manifest.demucs_ready, 'fixture must run demucs one --live').toBe(true);
	const stableId = manifest.stable_id;
	const playlistId = manifest.playlist_id;
	if (!stableId || !playlistId) {
		throw new Error('fixture manifest missing stable_id or playlist_id');
	}

	const tracksResponse = await request.get(
		`/api/v1/playlists/${playlistId}/tracks?hydrate=1`
	);
	expect(tracksResponse.ok()).toBe(true);
	const tracksBody = (await tracksResponse.json()) as {
		items?: { stable_id?: string; vocals?: { status?: string; regions?: unknown[] } }[];
	};
	const row = tracksBody.items?.find((item) => item.stable_id === stableId);
	expect(row, 'fixture track missing from hydrated playlist').toBeTruthy();
	expect(row?.vocals?.status).toBe('demucs');
	expect((row?.vocals?.regions?.length ?? 0) > 0).toBe(true);

	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await expect.poll(async () => page.locator(TRACK_ROW).count()).toBeGreaterThan(0);

	const targetRow = page.locator(`${TRACK_ROW}[data-stable-id="${stableId}"]`);
	await targetRow.scrollIntoViewIfNeeded();
	const strip = targetRow.getByTestId('preview-strip');
	await expect(strip).toBeVisible();

	const vocalPixels = await strip.evaluate((canvas) => {
		const el = canvas as HTMLCanvasElement;
		const ctx = el.getContext('2d');
		if (ctx === null) throw new Error('PreviewStrip canvas context unavailable');
		const { width, height } = el;
		const bandTop = Math.floor(height * 0.72);
		const data = ctx.getImageData(0, bandTop, width, height - bandTop).data;
		let hits = 0;
		for (let i = 0; i < data.length; i += 4) {
			const alpha = data[i + 3];
			if (alpha <= 0) continue;
			hits += 1;
		}
		return hits;
	});

	expect(vocalPixels, 'PreviewStrip must paint demucs vocal pixels').toBeGreaterThan(0);

	const sampleMatchesBlue = await strip.evaluate(
		([blueR, blueG, blueB]) => {
			const el = document.querySelector(
				'[data-testid="preview-strip"]'
			) as HTMLCanvasElement | null;
			if (el === null) return false;
			const ctx = el.getContext('2d');
			if (ctx === null) return false;
			const { width, height } = el;
			const bandTop = Math.floor(height * 0.72);
			const data = ctx.getImageData(0, bandTop, width, height - bandTop).data;
			for (let i = 0; i < data.length; i += 4) {
				const alpha = data[i + 3];
				if (alpha <= 0) continue;
				const r = data[i];
				const g = data[i + 1];
				const b = data[i + 2];
				if (
					Math.abs(r - blueR) <= 8 &&
					Math.abs(g - blueG) <= 8 &&
					Math.abs(b - blueB) <= 8
				) {
					return true;
				}
			}
			return false;
		},
		[VOCAL_BLUE.r, VOCAL_BLUE.g, VOCAL_BLUE.b] as const
	);
	expect(sampleMatchesBlue).toBe(true);
});
