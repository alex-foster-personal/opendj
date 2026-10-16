/**
 * The MIDI setting's disk-backed choice when the MIDI runtime chunk fails to
 * load (CHROME-07, Codex P2 4130152647 on PR #3896).
 *
 * settings/apply.ts loads midi-ui-state on demand; that module holds the usual
 * disk writer. When the chunk failed, the catch restored "off" in localStorage
 * only, so a disk-backed "on" survived and prefs hydration turned MIDI back on
 * at the next page load, reversing the user's "off".
 *
 * Root suite: the real engine's /api/v1/ui-prefs, the real library page and
 * Settings overlay, and page.route aborting the real midi-ui-state module URL.
 * The route aborts every request for that module, whether the toggle's
 * import() is the first or something at boot asked for it already (observed
 * here, referred by TopBar.svelte), so the toggle's import really fails.
 *
 * [if] a disable whose runtime chunk fails leaves disk "on" [then] stop.
 * [if] an enable whose runtime chunk fails ever writes "on" to disk [then] stop.
 * control [if] an enable whose chunk loads stops putting "on" on the wire
 *   through the same watcher [then] the "never writes on" test proves nothing.
 */
// requirement: CHROME-07
import { expect, test, type APIRequestContext, type Page, type Route } from '@playwright/test';

import { watchUiPrefsPuts, type UiPrefsPutWatch } from './support/ui-prefs-puts';

const RUNTIME = 'midi-ui-state.svelte.ts';

async function diskMidi(request: APIRequestContext): Promise<unknown> {
	const res = await request.get('/api/v1/ui-prefs');
	expect(res.ok()).toBeTruthy();
	return ((await res.json()) as { midi_enabled?: unknown }).midi_enabled;
}

async function setDiskMidi(request: APIRequestContext, value: boolean): Promise<void> {
	const res = await request.put('/api/v1/ui-prefs', { data: { midi_enabled: value } });
	expect(res.ok(), await res.text()).toBeTruthy();
	expect(await diskMidi(request)).toBe(value);
}

/** The midi_enabled values in a list of ui-prefs PUT bodies, in order. */
function midiValues(bodies: readonly Record<string, unknown>[]): unknown[] {
	return bodies.filter((body) => 'midi_enabled' in body).map((body) => body.midi_enabled);
}

/** The current test's page writes, settled by afterEach before it resets the disk. */
let activeWatch: UiPrefsPutWatch | null = null;

/** Watch the page's ui-prefs PUTs; afterEach waits for every one to be answered. */
function watchMidiPuts(page: Page): UiPrefsPutWatch {
	activeWatch = watchUiPrefsPuts(page);
	return activeWatch;
}

async function failRuntime(page: Page): Promise<string[]> {
	const failed: string[] = [];
	await page.route(
		(url) => url.pathname.endsWith(`/${RUNTIME}`),
		(route: Route) => {
			failed.push(route.request().url());
			return route.abort('failed');
		}
	);
	return failed;
}

async function midiToggle(page: Page) {
	await page.goto('/');
	const dialog = page.getByRole('dialog', { name: 'Settings' });
	// The chord's listener mounts with the layout; press until the overlay
	// answers rather than waiting on network quiet (the library polls).
	await expect(async () => {
		if (!(await dialog.isVisible())) await page.keyboard.press('Control+Comma');
		await expect(dialog).toBeVisible({ timeout: 2_000 });
	}).toPass({ timeout: 45_000 });
	await dialog.getByPlaceholder('Search settings...').fill('MIDI controllers');
	return dialog.locator('li.so-row', { hasText: 'MIDI controllers' }).getByRole('checkbox');
}

test.describe('MIDI setting on disk when the runtime chunk fails', () => {
	test.setTimeout(120_000);
	test.afterEach(async ({ request }) => {
		// The page is still open here (its fixture tears down after this hook),
		// and a PUT it already SENT can still be on its way through the proxy.
		// Reset the disk only once every one of them has been answered: a page
		// write that lands between this reset and the read below would make the
		// read return the page's value instead of ours.
		const watch = activeWatch;
		activeWatch = null;
		await watch?.settle();
		await setDiskMidi(request, false);
	});

	test('a failed-load disable writes off to disk, so a reload keeps it off', async ({ page, request }) => {
		await setDiskMidi(request, true);
		const failed = await failRuntime(page);
		const watch = watchMidiPuts(page);
		const toggle = await midiToggle(page);
		await expect(toggle, 'hydration read the disk-backed on').toBeChecked();

		await toggle.click();
		await expect.poll(() => diskMidi(request), { timeout: 15_000 }).toBe(false);
		expect(failed.length, 'the runtime chunk was requested and failed').toBeGreaterThan(0);
		expect(midiValues(watch.sent)).toEqual([false]);
		await expect(toggle).not.toBeChecked();

		// The reversal Codex described: disk hydration on the next load.
		const again = await midiToggle(page);
		await expect(again).not.toBeChecked();
	});

	test('a failed-load enable never writes on to disk', async ({ page, request }) => {
		await setDiskMidi(request, false);
		const failed = await failRuntime(page);
		const watch = watchMidiPuts(page);
		const toggle = await midiToggle(page);
		await expect(toggle).not.toBeChecked();

		await toggle.click();
		await expect.poll(() => midiValues(watch.sent), { timeout: 15_000 }).toEqual([false]);
		expect(failed.length).toBeGreaterThan(0);
		expect(await diskMidi(request)).toBe(false);
		await expect(toggle).not.toBeChecked();
	});

	test('control: an enable whose chunk loads writes on to disk the normal way', async ({ page, request }) => {
		// Makes the failed-load tests able to say no: with nothing blocked, the
		// same click, observed through the same PUT watcher, does put "on" on the
		// wire through midi-ui-state's own writer. Without this, "never writes on"
		// would also pass for a watcher that saw nothing or a click that did
		// nothing. Headless Chromium then denies WebMIDI, and requestMidiAccess's
		// catch clears the choice back to off (a later PUT); a disk-backed "on"
		// cannot be held long enough to test a loaded disable deterministically
		// here, so this control drives the enable instead.
		await setDiskMidi(request, false);
		const loaded: string[] = [];
		page.on('requestfinished', (r) => {
			if (new URL(r.url()).pathname.endsWith(`/${RUNTIME}`)) loaded.push(r.url());
		});
		const watch = watchMidiPuts(page);
		const toggle = await midiToggle(page);
		await expect(toggle).not.toBeChecked();

		await toggle.click();
		await expect.poll(() => midiValues(watch.sent)[0], { timeout: 15_000 }).toBe(true);
		// On the wire is not on disk: the title's claim holds only once the
		// engine has answered that PUT.
		await expect.poll(() => midiValues(watch.landed)[0], { timeout: 15_000 }).toBe(true);
		expect(loaded.length, 'the runtime chunk loaded, so this is the normal path').toBeGreaterThan(0);
	});
});
