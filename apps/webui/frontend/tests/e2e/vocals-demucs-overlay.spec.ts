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
	expect(manifest.demucs_ready, 'fixture must run demucs one --force').toBe(true);
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
		tracks?: {
			stable_id?: string;
			duration_ms?: number | null;
			vocals?: { status?: string; regions?: { start_s: number; end_s: number }[] };
		}[];
	};
	const row = tracksBody.tracks?.find((item) => item.stable_id === stableId);
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

	// PreviewStrip paints demucs regions as a VOCAL_BLUE bar along the TOP edge
	// of its canvas (VOCAL_BAR_H = 0.7 css px, alpha 0.5..1), positioned by
	// region / duration_ms, OVER the waveform. The bar blends with the waveform
	// color, so exact-color matching cannot see it (measured: 1 of 165
	// top-row pixels is pure blue). Measure it differentially instead: row 1
	// holds the waveform alone, so a top-row pixel is vocal-tinted when it moved
	// toward VOCAL_BLUE relative to the pixel below it (premultiplied RGB).
	// Tinted pixels inside the API's region span prove the overlay painted;
	// zero tinted pixels outside it proves the tint came from the regions.
	const durationMs = row?.duration_ms ?? null;
	expect(durationMs, 'hydrated row needs duration_ms to place vocal bars').not.toBeNull();
	const spans = (row?.vocals?.regions ?? []).map((region) => [
		(region.start_s * 1000) / (durationMs as number),
		(region.end_s * 1000) / (durationMs as number)
	]);
	const canvas = strip.locator('canvas');
	const countVocalTintedPixels = () =>
		canvas.evaluate(
			(el, { blue, regionSpans }) => {
				const cv = el as HTMLCanvasElement;
				const ctx = cv.getContext('2d');
				if (ctx === null) throw new Error('PreviewStrip canvas context unavailable');
				const top = ctx.getImageData(0, 0, cv.width, 1).data;
				const below = ctx.getImageData(0, 1, cv.width, 1).data;
				const premul = (d: Uint8ClampedArray, i: number) => {
					const a = d[i + 3] / 255;
					return [d[i] * a, d[i + 1] * a, d[i + 2] * a];
				};
				const distToBlue = (c: number[]) =>
					Math.hypot(c[0] - blue[0], c[1] - blue[1], c[2] - blue[2]);
				let inside = 0;
				let outside = 0;
				for (let x = 0; x < cv.width; x++) {
					const i = x * 4;
					const p0 = premul(top, i);
					const p1 = premul(below, i);
					const bluer = p0[2] - p0[0] - (p1[2] - p1[0]) >= 30;
					const closer = distToBlue(p1) - distToBlue(p0) >= 30;
					if (!(bluer && closer)) continue;
					const x0 = x / cv.width;
					const x1 = (x + 1) / cv.width;
					if (regionSpans.some(([a, b]) => x1 > a && x0 < b)) inside += 1;
					else outside += 1;
				}
				return { inside, outside, width: cv.width };
			},
			{ blue: [VOCAL_BLUE.r, VOCAL_BLUE.g, VOCAL_BLUE.b], regionSpans: spans }
		);
	// PreviewStrip redraws when vocals or DPR change, so poll the paint rather
	// than reading one early frame as "absent".
	await expect
		.poll(async () => (await countVocalTintedPixels()).inside, {
			message: 'PreviewStrip must paint demucs vocal pixels'
		})
		.toBeGreaterThan(0);
	const counts = await countVocalTintedPixels();
	console.log(
		`vocals-demucs-overlay tinted=${JSON.stringify(counts)} spans=${JSON.stringify(spans)}`
	);
	expect(counts.outside, 'vocal tint must appear only inside demucs regions').toBe(0);
});
