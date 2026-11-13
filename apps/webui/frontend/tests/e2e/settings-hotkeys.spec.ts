/**
 * Real-browser coverage for the settings chord from editable controls
 * (issue #12269, A11Y-01). The root Playwright config starts a real fixture
 * engine and Vite, so these keys reach the production listener that
 * settings/hotkeys.ts installs from the root layout.
 *
 * Acceptance:
 * - [if] focus is in a text input on /performance and Cmd+, or Ctrl+, does not open Settings [then stop]
 * - [if] Settings is open, its search field has focus and Escape does not close it [then stop]
 * - [if] the settings chord leaves its browser default action running [then stop]
 *
 * The node-level counterpart is tests/unit/settings-hotkeys.test.mjs.
 */
import { expect, test, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

const CHORDS = ['Meta+Comma', 'Control+Comma'] as const;

interface ChordProbe {
	code: string;
	defaultPrevented: boolean;
}

/** Record every Comma keydown as the page last saw it, after the app's own capture listener ran. */
async function _installChordProbe(page: Page): Promise<void> {
	await page.evaluate(() => {
		const seen: { code: string; defaultPrevented: boolean }[] = [];
		(window as Window & { __settingsChordProbe?: typeof seen }).__settingsChordProbe = seen;
		window.addEventListener(
			'keydown',
			(e) => {
				if (e.code === 'Comma') seen.push({ code: e.code, defaultPrevented: e.defaultPrevented });
			},
			true
		);
	});
}

/**
 * Wait until the boot pane has finished filling. With saved library tabs in
 * the engine's ui-prefs (any earlier spec in the same server session leaves
 * them), BrowserPanel's boot restore loads the saved tab and only then writes
 * that tab's search into the pane (library-tabs-controller _apply), which
 * would wipe text typed before the fill completes. An empty pane that has
 * not started a load also reads complete, so rows must be loaded too.
 */
async function _waitForBootPaneFill(page: Page): Promise<void> {
	await page.waitForFunction(() => {
		const load = window.musicDjToolsPerformance?.query().browser.load;
		return load?.complete === true && load.rows_loaded > 0;
	});
}

/** Read and clear the probe's recorded Comma keydowns. */
async function _drainChordProbe(page: Page): Promise<ChordProbe[]> {
	return page.evaluate(() => {
		const holder = window as Window & { __settingsChordProbe?: ChordProbe[] };
		const seen = holder.__settingsChordProbe ?? [];
		holder.__settingsChordProbe = [];
		return seen;
	});
}

for (const chord of CHORDS) {
	test(`${chord} opens Settings from a text input, Escape closes it`, async ({ page }) => {
		await page.goto('/performance');
		const search = page.locator('.rb-search input').first();
		await expect(search).toBeVisible({ timeout: 30_000 });
		await waitForPerformanceIpc(page);
		await _waitForBootPaneFill(page);
		await _installChordProbe(page);

		await search.fill('probe');
		await search.focus();
		await expect(search).toBeFocused();

		const settings = page.getByRole('dialog', { name: 'Settings' });
		await expect(settings).toBeHidden();
		await page.keyboard.press(chord);
		await expect(settings).toBeVisible({ timeout: 15_000 });

		const probes = await _drainChordProbe(page);
		expect(probes.length, 'the probe must see the chord keydown').toBeGreaterThan(0);
		for (const probe of probes) {
			expect(probe.defaultPrevented, `${chord} must prevent its browser default`).toBe(true);
		}
		await expect(search).toHaveValue('probe');

		const settingsSearch = page.getByRole('searchbox', { name: 'Search settings' });
		await expect(settingsSearch).toBeFocused();
		await page.keyboard.press('Escape');
		await expect(settings).toBeHidden();
	});
}
