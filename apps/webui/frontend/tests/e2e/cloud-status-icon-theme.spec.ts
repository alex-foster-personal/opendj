/**
 * Tidal streaming glyph color, measured in a real browser (CHROME-02).
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
 * [if] the Tidal glyph is not white on dark and black on light [then] stop.
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
}

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
			expect(await paint(page, '#tidal svg.provider-icon path', 'stroke')).toBe(TIDAL[theme]);
		});

		test('control: the other cloud states keep their existing colors', async ({ page }) => {
			await mount(page, theme);
			expect(await paint(page, '#remote svg.cloud-icon path >> nth=0', 'fill')).toBe('rgb(229, 72, 77)');
			expect(await paint(page, '#local svg.cloud-icon path >> nth=0', 'stroke')).toBe(DIM[theme]);
			expect(await paint(page, '#spotify svg.provider-icon circle', 'fill')).toBe('rgb(53, 192, 79)');
		});
	});
}
