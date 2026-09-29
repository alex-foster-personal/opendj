/**
 * MIDI drawer lazy loads: a failed chunk shows an error and retries on the
 * next open (CHROME-07), driven in a real browser.
 *
 * Codex P2 on PR #3896 (comment 4129741948): the learn-log pop-out import in
 * TopBar and MidiPanel's MidiDeviceList import had no rejection handling, so
 * a failed chunk fetch left an empty section or no pop-out, with nothing said.
 * Both now live in MidiPanel and follow MidiPanelLoader's contract.
 *
 * This bundles the REAL MidiPanel.svelte (and the real midi-ui-state, webmidi
 * and learn-log modules under it) with Svelte's client compiler. For the
 * failure cases the two lazy specifiers are left out of the bundle, so
 * Chromium's own import() rejects: "Failed to resolve module specifier". The
 * retry case then adds an import map that points those specifiers at a module
 * re-exporting the REAL components, so a real second import() succeeds only if
 * MidiPanel actually tries again. Nothing in MidiPanel is replaced.
 *
 * [if] a failed device-list import leaves the section silent [then] stop.
 * [if] a failed pop-out import leaves "pop out" doing nothing visible [then] stop.
 * [if] closing and reopening after a failure never retries [then] stop.
 * control [if] the retry fires on an ordinary re-render instead of the next
 *   open [then] stop.
 * control [if] the imports resolve [then] the real device list and pop-out
 *   render with no error [else stop].
 */
// requirement: CHROME-07
import { expect, test, type Page } from '@playwright/test';

import { bundleSvelteHarness } from './support/svelte-harness-bundle';

const DEVICE_LIST = '$lib/components/rb/midi/MidiDeviceList.svelte';
const POPOUT = '$lib/components/rb/midi/MidiLearnLogPopout.svelte';
const ORIGIN = 'http://midi-harness.test';

// The relative imports keep the real components in the bundle without going
// through the `$lib/...` specifiers that MidiPanel's import() calls use.
const HARNESS = `<script>
	import MidiPanel from '$lib/components/rb/MidiPanel.svelte';
	import DeviceList from './src/lib/components/rb/midi/MidiDeviceList.svelte';
	import LearnLogPopout from './src/lib/components/rb/midi/MidiLearnLogPopout.svelte';
	import { toggleMidiPanel } from '$lib/components/rb/midi/midi-ui-state.svelte';
	window.__harness = { toggle: toggleMidiPanel, DeviceList, LearnLogPopout };
</script>
<MidiPanel />`;

let failing = '';
let resolving = '';

test.beforeAll(async () => {
	failing = await bundleSvelteHarness(HARNESS, { unresolvable: [DEVICE_LIST, POPOUT] });
	resolving = await bundleSvelteHarness(HARNESS);
});

// A real http origin: the MIDI modules read localStorage, which about:blank denies.
async function mount(page: Page, script: string, fetched: string[] = []): Promise<void> {
	await page.route(`${ORIGIN}/**`, (route) => {
		const path = new URL(route.request().url()).pathname;
		fetched.push(path);
		if (path === '/') {
			return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html><body></body></html>' });
		}
		const name = path === '/device-list.js' ? 'DeviceList' : 'LearnLogPopout';
		return route.fulfill({
			contentType: 'text/javascript',
			body: `export default window.__harness.${name};`
		});
	});
	await page.goto(`${ORIGIN}/`);
	await page.addScriptTag({ content: script });
}

const toggle = (page: Page) =>
	page.evaluate(() => (window as unknown as { __harness: { toggle(): void } }).__harness.toggle());

// What a redeploy or a recovered network looks like to the next import():
// the same specifiers now resolve.
const restoreChunks = (page: Page) =>
	page.evaluate(
		({ deviceList, popout, origin }) => {
			const map = document.createElement('script');
			map.type = 'importmap';
			map.textContent = JSON.stringify({
				imports: { [deviceList]: `${origin}/device-list.js`, [popout]: `${origin}/learn-log-popout.js` }
			});
			document.head.appendChild(map);
		},
		{ deviceList: DEVICE_LIST, popout: POPOUT, origin: ORIGIN }
	);

const drawer = (page: Page) => page.getByRole('dialog', { name: 'MIDI devices and learn log' });
const deviceError = (page: Page) => drawer(page).getByRole('alert').filter({ hasText: 'Devices failed to load' });
const popoutError = (page: Page) => page.getByRole('alert').filter({ hasText: 'Learn log pop-out failed to load' });
const popout = (page: Page) => page.getByRole('log', { name: 'MIDI learn log' });

test.describe('MIDI drawer lazy loads', () => {
	test('a device list that fails to load says so inside the drawer', async ({ page }) => {
		await mount(page, failing);
		await toggle(page);
		await expect(drawer(page)).toBeVisible();
		await expect(deviceError(page)).toContainText('Failed to resolve module specifier');
		await expect(deviceError(page)).toContainText('Close and reopen MIDI to retry.');
		await expect(drawer(page).locator('.device-list')).toHaveCount(0);
	});

	test('a pop-out that fails to load says so, and Close dismisses the message', async ({ page }) => {
		await mount(page, failing);
		await toggle(page);
		await drawer(page).getByRole('button', { name: 'pop out' }).click();
		await expect(popoutError(page)).toContainText('Failed to resolve module specifier');
		await expect(popout(page)).toHaveCount(0);
		await popoutError(page).getByRole('button', { name: 'Close' }).click();
		await expect(popoutError(page)).toHaveCount(0);
	});

	test('both loads retry on the next open, not on a re-render while open', async ({ page }) => {
		const fetched: string[] = [];
		await mount(page, failing, fetched);
		await toggle(page);
		await expect(deviceError(page)).toBeVisible();

		await restoreChunks(page);
		// A re-render while still open (the width toggle) must not refetch.
		await drawer(page).getByRole('button', { name: 'expand to 70% viewport' }).click();
		await expect(drawer(page)).toHaveAttribute('data-width-mode', 'expanded');
		await page.waitForTimeout(300);
		await expect(deviceError(page)).toBeVisible();
		expect(fetched.filter((p) => p !== '/')).toEqual([]);

		await drawer(page).getByRole('button', { name: 'close', exact: true }).click();
		await expect(drawer(page)).toHaveCount(0);
		await toggle(page);
		await expect(drawer(page).locator('.device-list')).toBeVisible();
		await expect(deviceError(page)).toHaveCount(0);
		await drawer(page).getByRole('button', { name: 'pop out' }).click();
		await expect(popout(page)).toBeVisible();
		await expect(popoutError(page)).toHaveCount(0);
		expect(fetched.filter((p) => p !== '/').sort()).toEqual(['/device-list.js', '/learn-log-popout.js']);
	});

	test('control: when the imports resolve, the real device list and pop-out render', async ({ page }) => {
		await mount(page, resolving);
		await toggle(page);
		await expect(drawer(page).locator('.device-list')).toBeVisible();
		await drawer(page).getByRole('button', { name: 'pop out' }).click();
		await expect(popout(page)).toBeVisible();
		await expect(page.getByRole('alert')).toHaveCount(0);
	});
});
