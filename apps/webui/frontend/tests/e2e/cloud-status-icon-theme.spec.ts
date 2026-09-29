/**
 * Tidal streaming glyph color and shape, measured in a real browser (CHROME-02).
 *
 * Codex P2 on PR #3896 (comment 4129741953): the Tidal glyph stroked with
 * currentColor, so inside the cloud column (color: --rb-text-dim) it rendered
 * the same dim gray as every "not on CloudSync" cloud and did not read as a
 * provider. Tidal's mark is black and white, so the glyph is white on the dark
 * theme and black on the light one.
 *
 * This bundles the REAL CloudStatusIcon.svelte fed by the REAL trackCloudView(),
 * loads the REAL lib/rb/theme.css, and reads the computed colors Chromium
 * paints. The cell mirrors TrackTable's `.c-cloud` rule (color: var(--rb-text-dim)).
 *
 * Codex P2 on PR #3896 (comment 4130498592): the glyph was two stroked
 * chevrons, not Tidal's mark of four filled diamonds (three touching across the
 * top, one under the middle). The shape is read from Chromium's own geometry:
 * the painted bbox and SVGGeometryElement.isPointInFill at each diamond's
 * center and at the empty cells beside the lower one.
 *
 * [if] the Tidal glyph is not white on dark and black on light [then] stop.
 * [if] the Tidal glyph is not four filled diamonds in that layout [then] stop.
 * [if] the SoundCloud glyph is not SoundCloud's orange mark (four bars rising
 *   into a flat-based cloud) but the old ECG zigzag or any other line [then]
 *   stop (Codex P2 4130581626).
 * control [if] on-cloud-not-local stops being red, not-on-cloud stops
 *   inheriting the dim cell color, or Spotify loses its green [then] stop:
 *   the fix leaked past the Tidal glyph.
 */
// requirement: CHROME-02
import { expect, test, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

import { bundleSvelteHarness } from './support/svelte-harness-bundle';

const THEME_CSS = fileURLToPath(new URL('../../src/lib/rb/theme.css', import.meta.url));

const HARNESS = `<script>
	import CloudStatusIcon from '$lib/components/rb/browser/CloudStatusIcon.svelte';
	import { trackCloudView } from '$lib/components/rb/browser/track-cloud-state';
	const base = { transfer: null, spotifyPending: false };
	const views = {
		tidal: trackCloudView({ ...base, fileExists: false, isStreaming: true, hasRemoteCopy: false, folderPath: 'tidal:track:1' }),
		spotify: trackCloudView({ ...base, fileExists: false, isStreaming: true, hasRemoteCopy: false, folderPath: 'spotify:track:1' }),
		soundcloud: trackCloudView({ ...base, fileExists: false, isStreaming: true, hasRemoteCopy: false, folderPath: 'soundcloud:track:1' }),
		remote: trackCloudView({ ...base, fileExists: false, isStreaming: false, hasRemoteCopy: true }),
		local: trackCloudView({ ...base, fileExists: true, isStreaming: false, hasRemoteCopy: false })
	};
</script>
<div class="perf-root">
	<table><tbody><tr>
		{#each Object.entries(views) as [id, view] (id)}
			<td id={id} style="color: var(--rb-text-dim)"><CloudStatusIcon {view} /></td>
		{/each}
	</tr></tbody></table>
</div>`;

let script = '';
let themeCss = '';

test.beforeAll(async () => {
	script = await bundleSvelteHarness(HARNESS);
	themeCss = await readFile(THEME_CSS, 'utf8');
});

async function mount(page: Page, theme: 'dark' | 'light'): Promise<void> {
	await page.setContent(`<!doctype html><html data-theme="${theme}"><body></body></html>`);
	await page.addStyleTag({ content: themeCss });
	await page.addScriptTag({ content: script });
	await expect(page.locator('#tidal svg.provider-icon path')).toHaveCount(1);
	await expect(page.locator('#soundcloud svg.provider-icon path')).toHaveCount(1);
}

/** Chromium's own geometry for one glyph path: its bbox, its closed
 * subpaths, and SVGGeometryElement.isPointInFill at each probe point. */
const geometry = (page: Page, selector: string, probes: [number, number][]) =>
	page.locator(selector).evaluate((el, pts) => {
		const path = el as SVGPathElement;
		const b = path.getBBox();
		return {
			bbox: [b.x, b.y, b.width, b.height].map((n) => Math.round(n * 100) / 100),
			closedSubpaths: (path.getAttribute('d')?.match(/z/gi) ?? []).length,
			inFill: pts.map(([x, y]) => path.isPointInFill(new DOMPoint(x, y)))
		};
	}, probes);

const paint = (page: Page, selector: string, prop: 'stroke' | 'fill' | 'color') =>
	page.locator(selector).evaluate((el, p) => getComputedStyle(el).getPropertyValue(p), prop);

const DIM = { dark: 'rgb(131, 137, 144)', light: 'rgb(93, 87, 78)' } as const;
const TIDAL = { dark: 'rgb(255, 255, 255)', light: 'rgb(0, 0, 0)' } as const;

for (const theme of ['dark', 'light'] as const) {
	test.describe(`${theme} theme`, () => {
		test('the Tidal glyph takes the theme color, not the dim column text', async ({ page }) => {
			await mount(page, theme);
			// The cell really is dim, so a currentColor glyph would be dim too.
			expect(await paint(page, '#tidal', 'color')).toBe(DIM[theme]);
			expect(await paint(page, '#tidal svg.provider-icon path', 'fill')).toBe(TIDAL[theme]);
			expect(await paint(page, '#tidal svg.provider-icon path', 'stroke')).toBe('none');
		});

		test("the Tidal glyph is Tidal's four-diamond mark", async ({ page }) => {
			await mount(page, theme);
			const shape = await page.locator('#tidal svg.provider-icon path').evaluate((el) => {
				const path = el as SVGPathElement;
				const b = path.getBBox();
				const inFill = (x: number, y: number): boolean => {
					const pt = new DOMPoint(x, y);
					return path.isPointInFill(pt);
				};
				return {
					bbox: [b.x, b.y, b.width, b.height],
					closedSubpaths: (path.getAttribute('d')?.match(/z/gi) ?? []).length,
					// The four diamond centers, then the cells where no diamond is.
					centers: [inFill(3, 5.5), inFill(8, 5.5), inFill(13, 5.5), inFill(8, 10.5)],
					empty: [inFill(3, 10.5), inFill(13, 10.5), inFill(8, 1.5), inFill(8, 14.5)]
				};
			});
			expect(shape.bbox).toEqual([0.5, 3, 15, 10]);
			expect(shape.closedSubpaths).toBe(4);
			expect(shape.centers).toEqual([true, true, true, true]);
			expect(shape.empty).toEqual([false, false, false, false]);
		});

		test("the SoundCloud glyph is SoundCloud's orange bars-into-cloud mark", async ({ page }) => {
			await mount(page, theme);
			const sel = '#soundcloud svg.provider-icon path';
			// The brand orange on both themes, filled, not a stroked line.
			expect(await paint(page, sel, 'fill')).toBe('rgb(255, 85, 0)');
			expect(await paint(page, sel, 'stroke')).toBe('none');
			const shape = await geometry(page, sel, [
				// Inside: the four bars, left to right, then the cloud's big and small bumps.
				[1.1, 11], [2.7, 10], [4.3, 9], [5.9, 8], [10, 7], [14.2, 10],
				// Outside: the gaps between bars, above the shortest bar, between
				// the last bar and the cloud, and above the cloud.
				[1.9, 11], [3.5, 11], [1.1, 8], [6.7, 11], [10, 3.5]
			]);
			expect(shape.closedSubpaths).toBe(5);
			expect(shape.bbox).toEqual([0.6, 4.57, 14.65, 7.43]);
			expect(shape.inFill).toEqual([true, true, true, true, true, true, false, false, false, false, false]);
		});

		test('control: the other cloud states keep their existing colors', async ({ page }) => {
			await mount(page, theme);
			expect(await paint(page, '#remote svg.cloud-icon path >> nth=0', 'fill')).toBe('rgb(229, 72, 77)');
			expect(await paint(page, '#local svg.cloud-icon path >> nth=0', 'stroke')).toBe(DIM[theme]);
			expect(await paint(page, '#spotify svg.provider-icon circle', 'fill')).toBe('rgb(53, 192, 79)');
		});
	});
}
