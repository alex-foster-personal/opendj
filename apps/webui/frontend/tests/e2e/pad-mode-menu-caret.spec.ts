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

import { bundleSvelteHarness, readSrcForHarness } from './support/svelte-harness-bundle';

const THEME_CSS = 'src/lib/rb/theme.css';

// prefs.svelte.ts applies the stored theme and skin to <html> at load, and
// with empty storage those are the app defaults: dark, and Gothic (mono-dev),
// which repaints both themes. The harness picks both through the app's own
// setters, so each case presents exactly what a DJ sees with them chosen (a
// data-theme written into the page's HTML is overwritten at load).
const HARNESS = `<script>
	import PadModeMenu from '$lib/components/rb/deck/PadModeMenu.svelte';
	import { setTheme, setUiSkin } from '$lib/rb/prefs.svelte';
	const skin = window.__padMenuHarnessSkin;
	const theme = window.__padMenuHarnessTheme;
	if (skin === undefined) throw new Error('pad menu harness: window.__padMenuHarnessSkin is unset');
	if (theme === undefined) throw new Error('pad menu harness: window.__padMenuHarnessTheme is unset');
	setTheme(theme);
	setUiSkin(skin);
</script>
<div class="perf-root" style="padding: 40px"><PadModeMenu deckId={1} /></div>`;

type Skin = 'default' | 'mono-dev';
type Theme = 'dark' | 'light';

// --rb-text-dim per skin and theme (theme.css): Default follows the theme,
// Gothic paints #8c8c8c in both.
const CASES: { skin: Skin; theme: Theme; dim: string }[] = [
	{ skin: 'default', theme: 'dark', dim: 'rgb(131, 137, 144)' },
	{ skin: 'default', theme: 'light', dim: 'rgb(93, 87, 78)' },
	{ skin: 'mono-dev', theme: 'dark', dim: 'rgb(140, 140, 140)' }
];

let script = '';
let themeCss = '';

test.beforeAll(async () => {
	script = await bundleSvelteHarness(HARNESS);
	themeCss = await readSrcForHarness(THEME_CSS);
});

// PadModeMenu dispatches through performance-ipc (843a7643396, PR #11718), whose
// module graph reads localStorage at load (prefs.svelte.ts). about:blank, which
// page.setContent writes into, has an opaque origin that denies storage, so the
// bundle threw before mount. Serve the page from a real origin instead.
const ORIGIN = 'https://pad-mode-menu.invalid';

async function mount(page: Page, theme: Theme, skin: Skin): Promise<void> {
	await page.route(`${ORIGIN}/`, (route) =>
		route.fulfill({
			status: 200,
			contentType: 'text/html',
			body: `<!doctype html><html data-theme="${theme}"><body></body></html>`
		})
	);
	await page.goto(`${ORIGIN}/`);
	await page.addStyleTag({ content: themeCss });
	await page.evaluate(
		([s, t]) => {
			const host = window as unknown as {
				__padMenuHarnessSkin: string;
				__padMenuHarnessTheme: string;
			};
			host.__padMenuHarnessSkin = s;
			host.__padMenuHarnessTheme = t;
		},
		[skin, theme] as const
	);
	await page.addScriptTag({ content: script });
	await expect(page.getByTestId('hot-cue-menu-deck-1')).toBeVisible();
}

for (const { skin, theme, dim } of CASES) {
	const name = skin === 'default' ? theme : `${skin} ${theme}`;
	test(`${name}: the caret is a painted CSS triangle with no text`, async ({ page }) => {
		await mount(page, theme, skin);
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
		expect(drawn.top).toEqual(['4px', 'solid', dim]);
		expect(drawn.sides).toEqual(['3px', '3px', 'rgba(0, 0, 0, 0)', 'rgba(0, 0, 0, 0)']);
	});
}

test('control: the menu still opens and lists every pad mode', async ({ page }) => {
	await mount(page, 'dark', 'default');
	await page.getByTestId('hot-cue-menu-deck-1').click();
	await expect(page.getByRole('menu')).toBeVisible();
	await expect(page.getByRole('menuitem')).toHaveCount(8);
});
