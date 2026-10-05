/**
 * The HOT CUE pad-mode menu's caret is a CSS triangle, not a text glyph
 * (CHROME-01, Codex P2 4131234974: it was `&#9662;`, U+25BE drawn from a
 * font, which the literal-character sweep never saw).
 *
 * Bundles the REAL PadModeMenu.svelte with its own styles and the REAL
 * lib/rb/theme.css, then reads what Chromium lays out and paints.
 *
 * [if] the caret carries any text (a glyph by any spelling) [then] stop.
 * [if] the caret paints nothing, or not the dim theme color [then] stop: the
 *   menu lost its affordance rather than changing how it is drawn.
 * control [if] the menu no longer opens with every pad mode [then] stop.
 */
// requirement: CHROME-01
import { expect, test, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

import { bundleSvelteHarness } from './support/svelte-harness-bundle';

const THEME_CSS = fileURLToPath(new URL('../../src/lib/rb/theme.css', import.meta.url));

const HARNESS = `<script>
	import PadModeMenu from '$lib/components/rb/deck/PadModeMenu.svelte';
</script>
<div class="perf-root" style="padding: 40px"><PadModeMenu deckId={1} /></div>`;

const DIM = { dark: 'rgb(131, 137, 144)', light: 'rgb(93, 87, 78)' } as const;

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
	await expect(page.getByTestId('hot-cue-menu-deck-1')).toBeVisible();
}

for (const theme of ['dark', 'light'] as const) {
	test(`${theme}: the caret is a painted CSS triangle with no text`, async ({ page }) => {
		await mount(page, theme);
		const trigger = page.getByTestId('hot-cue-menu-deck-1');
		expect((await trigger.innerText()).trim()).toBe('HOT CUE');
		// The caret is the trigger's ::after: no element, no text node.
		const drawn = await trigger.evaluate((el) => {
			const cs = getComputedStyle(el, '::after');
			return {
				content: cs.content,
				display: cs.display,
				box: [cs.width, cs.height],
				top: [cs.borderTopWidth, cs.borderTopStyle, cs.borderTopColor],
				sides: [cs.borderLeftWidth, cs.borderRightWidth, cs.borderLeftColor, cs.borderRightColor],
				text: el.textContent?.trim()
			};
		});
		expect(drawn.text).toBe('HOT CUE');
		expect(drawn.content, 'an empty generated box, no glyph').toBe('""');
		expect(drawn.display).toBe('inline-block');
		// 3px + 3px transparent sides over a 4px colored top: a down triangle.
		expect(drawn.box).toEqual(['0px', '0px']);
		expect(drawn.top).toEqual(['4px', 'solid', DIM[theme]]);
		expect(drawn.sides).toEqual(['3px', '3px', 'rgba(0, 0, 0, 0)', 'rgba(0, 0, 0, 0)']);
	});
}

test('control: the menu still opens and lists every pad mode', async ({ page }) => {
	await mount(page, 'dark');
	await page.getByTestId('hot-cue-menu-deck-1').click();
	await expect(page.getByRole('menu')).toBeVisible();
	await expect(page.getByRole('menuitem')).toHaveCount(8);
});
