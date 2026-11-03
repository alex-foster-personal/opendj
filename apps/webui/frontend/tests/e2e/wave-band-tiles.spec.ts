// requirement: WAVEZOOM-01
// [if] the cached band image stops painting past its first tile in a real browser [then] this fails, [else stop].
// [if] two band tiles meet with a gap or a faint seam, in tri-band or the line design [then] this fails, [else stop].
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

/**
 * WAVEZOOM-01 in a REAL browser canvas (Codex P1 on PR #6786): the band
 * cache is split into TILE_PX-wide tiles, and this proves the tiles a real
 * Chromium composites under the playhead paint the track continuously across
 * a tile edge. The unit suite checks the tile arithmetic; only a browser can
 * say what the pixels look like.
 *
 * Real path end to end: the root config's fixture library (real WAV files,
 * analyzed by the engine), loaded through the performance dispatcher, painted
 * by WaveRow. The fixture audio is a continuous tone plus beat pulses, so
 * every column of the row has signal (the quiet tone paints a thin band of
 * about 2 px at the default row height) and an empty column is a real defect.
 *
 * The row is made wide (3840 px viewport) so a 60 s fixture at the default
 * zoom is wider than one tile, and the playhead is parked 2 s before the
 * first tile edge so that edge sits in view, clear of the playhead line. The
 * window and playhead column are read from the row's own data attributes
 * (WAVEZOOM-6802/04), so the arithmetic follows whatever the zoom shows.
 */

/** Mirrors BAND_CACHE_CFG.TILE_PX (render.ts). */
const TILE_PX = 4095;
/** Device columns per measured bin (see the bin note in the test). */
const BIN_COLS = 4;
/** Bins either side of the edge that must be painted like their neighbors. */
const EDGE_SPAN = 3;
/** An edge bin must carry at least this share of a typical bin's paint. */
const EDGE_FLOOR = 0.6;

test.use({ viewport: { width: 3840, height: 1000 } });
test.setTimeout(120_000);

async function performanceReady(page: Page): Promise<void> {
	await page.goto('/performance');
	await expect(page.getByLabel('Open DJ launch animation')).toBeHidden({
		timeout: 30_000
	});
	await page.waitForFunction(() => window.musicDjToolsPerformance !== undefined, null, { timeout: 60_000 });
}

for (const design of ['tri-band', 'line'] as const) {
	test(`${design}: band tiles paint the track continuously across a tile edge`, async ({ page, request }) => {
		const listing = await request.get('/api/v1/tracks?limit=50&available=true');
		expect(listing.ok(), 'track listing must succeed').toBeTruthy();
		const { items } = (await listing.json()) as {
			items: { stable_id: string; file_exists: boolean }[];
		};
		const track = items.find((item) => item.file_exists);
		expect(track, 'the fixture library carries an on-disk track').toBeDefined();

		await performanceReady(page);
		await page.evaluate(async (d) => {
			await window.musicDjToolsPerformance!.dispatch({
				type: 'set_waveform_design',
				design: d
			});
		}, design);
		expect(
			await page.evaluate(() => window.musicDjToolsPerformance!.query().ui.waveform_design_effective),
			'the row paints in the design under test'
		).toBe(design);
		await page.evaluate(async (sid) => {
			await window.musicDjToolsPerformance!.dispatch({
				type: 'load',
				deck: 1,
				stable_id: sid
			});
		}, track!.stable_id);
		const canvas = page.locator('.rb-waverow[data-deck="1"] canvas.wave-seek-canvas');
		await expect(canvas).toHaveAttribute('aria-valuemax', /^[1-9]\d*$/, {
			timeout: 30_000
		});

		const deck = await page.evaluate(() => window.musicDjToolsPerformance!.query().decks[1]);
		expect(deck.pitch, 'an unsynced deck plays at native speed').toBe(1);
		const durationS = (deck.duration_ms ?? 0) / 1000;
		const widthCss = await canvas.evaluate((el) => el.getBoundingClientRect().width);
		const row = page.locator('.rb-waverow[data-deck="1"]');
		const windowS = Number(await row.getAttribute('data-wave-window-s'));
		expect(windowS, 'the row publishes its window').toBeGreaterThan(0);
		const pxPerS = widthCss / windowS;
		// Positive control on the regime: the whole-track image really spans more
		// than one tile, or this test would prove nothing about tiling.
		expect(Math.ceil(durationS * pxPerS), `${durationS} s at ${pxPerS.toFixed(1)} px/s`).toBeGreaterThan(TILE_PX + 200);

		const edgeS = TILE_PX / pxPerS;
		const positionS = edgeS - 2;
		await page.evaluate(
			async (ms) => {
				await window.musicDjToolsPerformance!.dispatch({
					type: 'seek',
					deck: 1,
					position_ms: ms
				});
			},
			Math.round(positionS * 1000)
		);
		await expect
			.poll(async () => page.evaluate(() => window.musicDjToolsPerformance!.query().decks[1].position_ms), {
				timeout: 10_000
			})
			.toBeGreaterThan(positionS * 1000 - 50);
		// Two frames so the row repaints at the new position.
		await page.evaluate(
			() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
		);

		const actualS = (await page.evaluate(() => window.musicDjToolsPerformance!.query().decks[1].position_ms)) / 1000;
		const frac = Number(await row.getAttribute('data-wave-playhead-frac'));
		expect(frac, 'the row publishes its playhead column').toBeGreaterThan(0);
		const tLeft = actualS - windowS * frac;
		const edgeCss = (edgeS - tLeft) * pxPerS;
		expect(edgeCss, 'the tile edge is in view, right of the playhead').toBeGreaterThan(widthCss * frac + 20);
		expect(edgeCss).toBeLessThan(widthCss - 200);

		// Painted-pixel count per device column, read back from the real canvas.
		const columns = await canvas.evaluate((el, edge) => {
			const c = el as HTMLCanvasElement;
			const dpr = c.width / c.getBoundingClientRect().width;
			const ctx = c.getContext('2d');
			if (ctx === null) throw new Error('row canvas has no 2d context');
			const x0 = Math.max(0, Math.floor((edge - 60) * dpr));
			const x1 = Math.min(c.width, Math.ceil((edge + 260) * dpr));
			const data = ctx.getImageData(x0, 0, x1 - x0, c.height).data;
			const w = x1 - x0;
			const bg = [data[0], data[1], data[2]];
			const counts: number[] = [];
			for (let x = 0; x < w; x++) {
				let n = 0;
				for (let y = 0; y < c.height; y++) {
					const i = (y * w + x) * 4;
					const d = Math.abs(data[i] - bg[0]) + Math.abs(data[i + 1] - bg[1]) + Math.abs(data[i + 2] - bg[2]);
					if (d > 24) n++;
				}
				counts.push(n);
			}
			return { counts, x0, dpr };
		}, edgeCss);

		const edgeIdx = Math.round(edgeCss * columns.dpr) - columns.x0;
		// The fixture's detail points are sparser than the row's columns at this
		// width, so about every third column is legitimately empty (measured: the
		// same rhythm far from any edge). The unit is therefore a BIN of
		// BIN_COLS columns: every bin from just left of the edge to the end of the
		// read span must carry paint, and the bins straddling the edge must carry
		// as much as the typical bin around them.
		const bins: number[] = [];
		const firstBin = edgeIdx - EDGE_SPAN * BIN_COLS;
		for (let b = firstBin; b + BIN_COLS <= columns.counts.length; b += BIN_COLS) {
			bins.push(columns.counts.slice(b, b + BIN_COLS).reduce((a, n) => a + n, 0));
		}
		const median = (values: number[]): number => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
		const typical = median(bins);
		expect(typical, 'the row around the edge carries waveform paint').toBeGreaterThanOrEqual(2);
		for (let k = 0; k < 2 * EDGE_SPAN; k++) {
			expect(
				bins[k],
				`bin ${k - EDGE_SPAN} from the tile edge has ${bins[k]} painted px vs median ${typical}`
			).toBeGreaterThanOrEqual(Math.max(1, Math.floor(typical * EDGE_FLOOR)));
		}
		// The second tile is drawn at all: every bin right of the edge is painted.
		const blank = bins.filter((n) => n === 0).length;
		expect(blank, `${blank} of ${bins.length} bins from the edge onward are blank`).toBe(0);
	});
}
