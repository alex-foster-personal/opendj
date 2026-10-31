// requirement: LIBWS-01
// requirement: LIBWS-02
// requirement: LIBWS-03
// requirement: LIBWS-04
// requirement: LIBWS-06
// requirement: PREF-01
import { test, expect, type Page } from '@playwright/test';

import { spendBootLanding } from './support/boot-landing';

const ROW = '[data-testid="track-row"]';

async function openCrateDesk(page: Page): Promise<void> {
	await page.goto('/');
	// The Crate Desk is a dynamic import from "/", so a cold dev server compiles
	// it (and the shared BrowserPanel) on the first open: allow the same 30s the
	// first row gets rather than the 5s default.
	await expect(page.getByTestId('library-workspace')).toBeVisible({ timeout: 30_000 });
	await expect(page.locator(ROW).first()).toBeVisible({ timeout: 30_000 });
}

/** Click the row on its title text, clear of the rating and preview cells. */
async function selectRow(page: Page, index: number, modifiers: Array<'Shift'> = []): Promise<void> {
	await page.locator(ROW).nth(index).locator('td.c-title').click({ modifiers });
}

/** Poll Gig's IPC until decks 1-4 hold exactly these tracks. */
async function expectGigDecks(page: Page, expected: Array<string | null>): Promise<void> {
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, { timeout: 30_000 });
	await expect
		.poll(
			() =>
				page.evaluate(() =>
					[1, 2, 3, 4].map(
						(deckId) => window.musicDjToolsPerformance?.query().decks[deckId as 1 | 2 | 3 | 4]?.stable_id ?? null
					)
				),
			{ message: 'Gig never loaded the requested track onto the requested deck', timeout: 30_000 }
		)
		.toEqual(expected);
}

test.describe('LIBWS Crate Desk library page', () => {
	// Every `page.goto('/')` below is a cold open and would race PERFMODE-11's
	// one-shot redirect into Gig; support/boot-landing.ts has the full account.
	test.beforeEach(async ({ page }) => {
		await spendBootLanding(page);
	});

	test('mounts the shared browser full-window with the inspector beside it', async ({ page }) => {
		await openCrateDesk(page);
		await expect(page.getByTestId('browser-panel')).toBeVisible();
		await expect(page.getByTestId('track-inspector')).toBeVisible();
		await expect(page.getByTestId('inspector-empty')).toBeVisible();
		// Full-bleed: the app-shell sidebar is gone; on main (PERF-UI-07 removed
		// the compact app nav) the top bar's Pages menu carries its links.
		await expect(page.locator('.app-shell')).toHaveCount(0);
		await expect(page.getByTestId('performance-app-nav')).toHaveCount(0);
		const pages = page.getByTestId('library-pages-menu');
		await pages.locator('summary').click();
		await expect(pages.locator('a[href="/reconcile"]')).toBeVisible();
		await expect(pages.locator('a[href="/"]')).toHaveAttribute('aria-current', 'page');
		await pages.locator('summary').click();
		// LIBWS-05 is planned, so the Cmd-K entry is inert and says so.
		const quick = page.getByTestId('library-quick-select');
		await expect(quick).toBeDisabled();
		await expect(quick).toHaveAttribute('title', 'not implemented - see PARITY-TODO');
	});

	test('selecting a track fills the inspector and preview bar from the real track', async ({ page }) => {
		await openCrateDesk(page);
		const firstRow = page.locator(ROW).first();
		const stableId = await firstRow.getAttribute('data-stable-id');
		if (!stableId) throw new Error('first row is missing data-stable-id');
		const track = await (await page.request.get(`/api/v1/tracks/${stableId}`)).json();

		await selectRow(page, 0);
		await expect(page.getByTestId('inspector-title')).toHaveText(track.title ?? '(untitled)');
		await expect(page.getByTestId('library-preview-title')).toHaveText(track.title ?? '(untitled)');
		await expect(page.getByTestId('library-selection-count')).toHaveText('1 selected');

		await page.getByRole('link', { name: 'Open full track page' }).click();
		await expect(page).toHaveURL(new RegExp(`/track/${stableId}`));
	});

	test('a multi-selection shows a summary, not one track', async ({ page }) => {
		await openCrateDesk(page);
		const count = await page.locator(ROW).count();
		test.skip(count < 2, 'fixture library needs at least two tracks');
		await selectRow(page, 0);
		await selectRow(page, 1, ['Shift']);
		await expect(page.getByTestId('inspector-multi-heading')).toHaveText('2 tracks selected');
		await expect(page.getByTestId('library-selection-count')).toHaveText('2 selected');
	});

	test('Library mode builds no Gig engine, and a double-click previews instead of loading', async ({ page }) => {
		await openCrateDesk(page);
		await selectRow(page, 0);
		const row = page.locator(ROW).first();

		const probe = await page.evaluate(async () => {
			const mod = await import(new URL('/src/lib/rb/library-mode-runtime.ts', location.href).href);
			return mod.readLibraryModeIdleProbe();
		});
		expect(probe.audio_context_count).toBe(0);
		expect(probe.deck_nodes_present).toBe(false);
		expect(probe.prefetch_ready_count).toBe(0);

		// Fail closed: a fixture row without local audio would skip the real path.
		await expect(page.getByTestId('library-preview-play'), 'the first fixture row must have local audio').toBeEnabled();
		await row.locator('td.c-title').dblclick();
		// Still the Crate Desk: the path stays `/` (the pane may add its own query).
		await expect(page).toHaveURL(/^https?:\/\/[^/]+\/(\?[^#]*)?$/);
		// The preview player owns the track now, whatever the fixture's audio does.
		await expect(page.getByTestId('library-preview-play')).not.toHaveAttribute('data-preview-status', 'idle');
		const after = await page.evaluate(async () => {
			const mod = await import(new URL('/src/lib/rb/library-mode-runtime.ts', location.href).href);
			return mod.readLibraryModeIdleProbe();
		});
		expect(after.deck_nodes_present).toBe(false);
	});

	test('the preview bar plays real audio, and stays stoppable after the selection moves', async ({ page }) => {
		await openCrateDesk(page);
		await selectRow(page, 0);
		const play = page.getByTestId('library-preview-play');
		await expect(play).not.toHaveAttribute('title', 'not implemented - see PARITY-TODO');
		// Fail closed (Codex P1 on PR #6189): no local audio on the fixture row
		// means this acceptance did not run, which must read red, never skipped.
		await expect(play, 'the first fixture row must have local audio').toBeEnabled();
		await play.click();
		// Real media through the real fetch, decode and Web Audio path: it must
		// reach playing, not merely leave idle (loading would pass that).
		await expect(play).toHaveAttribute('data-preview-status', 'playing', { timeout: 30_000 });

		// Codex P1 on PR #6189: moving the selection must not strand the audible
		// preview. The bar keeps a Stop for it, and Stop silences it.
		expect(await page.locator(ROW).count(), 'the fixture library must list a second row').toBeGreaterThanOrEqual(2);
		await selectRow(page, 1);
		const stopOther = page.getByTestId('library-preview-stop-other');
		await expect(stopOther).toBeVisible();
		await expect(stopOther).toHaveAttribute('data-preview-status', 'playing');
		await stopOther.click();
		await expect(stopOther).toHaveCount(0);
		await selectRow(page, 0);
		await expect(play).toHaveAttribute('data-preview-status', 'idle');
	});

	test('Load to deck from Library opens Gig with that track on that deck', async ({ page }) => {
		await openCrateDesk(page);
		const row = page.locator(ROW).first();
		const stableId = await row.getAttribute('data-stable-id');
		if (!stableId) throw new Error('first row is missing data-stable-id');
		await row.locator('td.c-title').click({ button: 'right' });
		const load2 = page.getByTestId('context-menu-load-deck-2');
		await expect(load2).toBeVisible();
		await expect(load2, 'the first fixture row must have local audio').toBeEnabled();
		await load2.click();
		await expect(page).toHaveURL(new RegExp(`/performance\\?.*d2=${encodeURIComponent(stableId)}`));
		// The URL alone proves nothing about the deck: Gig must actually load it.
		await expectGigDecks(page, [null, stableId, null, null]);
	});

	// Both ways out of Library to Gig: the mode picker, and the PAGES menu's
	// Performance link (Codex P1s on PR #6189).
	const LIBRARY_EXITS = [
		{ via: 'the mode picker', menu: 'details.mode-picker', link: 'a.mode-card[href="/performance"]' },
		{ via: 'the PAGES menu', menu: 'details.pages-menu', link: 'nav.pages-list a[href="/performance"]' }
	];
	for (const exit of LIBRARY_EXITS) {
		test(`leaving a directly opened Library for Gig through ${exit.via} does not replay the old session`, async ({ page }) => {
			// A Gig set to come back to: deck 1 loaded, so its session snapshot names it.
			await openCrateDesk(page);
			const stableId = await page.locator(ROW).first().getAttribute('data-stable-id');
			if (!stableId) throw new Error('first row is missing data-stable-id');
			await page.goto(`/performance?d1=${encodeURIComponent(stableId)}`);
			await expectGigDecks(page, [stableId, null, null, null]);

			// Codex P1 on PR #6189: Library opened directly (a typed `/`), not through
			// Gig's picker, so only the Library-side exit can mark it. Record the
			// marker as it is written: Gig consumes it on arrival.
			await page.goto('/');
			await expect(page.getByTestId('library-workspace')).toBeVisible({ timeout: 30_000 });
			await page.evaluate(() => {
				const seen: string[] = [];
				Reflect.set(window, '__libraryExitKeys', seen);
				const set = Storage.prototype.setItem;
				Storage.prototype.setItem = function (key: string, value: string) {
					if (this === window.sessionStorage) seen.push(key);
					return set.call(this, key, value);
				};
			});
			expect(await page.evaluate(() => sessionStorage.getItem('mdt.library_rescue_hold'))).toBeNull();
			const menu = page.locator(exit.menu);
			await menu.locator('summary').click();
			await menu.locator(exit.link).click();
			await expect(page).toHaveURL(/\/performance(\?|$)/);
			const marked = await page.evaluate(() => Reflect.get(window, '__libraryExitKeys') as string[]);
			expect(marked).toEqual(expect.arrayContaining(['mdt.library_mode_exit', 'mdt.library_rescue_hold']));
			// The hold lifts once Gig's deck restore settles; only then is "empty" a verdict.
			await expect
				.poll(() => page.evaluate(() => sessionStorage.getItem('mdt.library_rescue_hold')), { timeout: 30_000 })
				.toBeNull();
			await expectGigDecks(page, [null, null, null, null]);
		});
	}

	test('browser Forward from Library to Gig marks the Library exit too', async ({ page }) => {
		// Codex P1 on PR #6189: history navigation calls neither click handler.
		await page.goto('/');
		await expect(page.getByTestId('library-workspace')).toBeVisible({ timeout: 30_000 });
		const picker = page.locator('details.mode-picker');
		await picker.locator('summary').click();
		await picker.locator('a.mode-card[href="/performance"]').click();
		await expect(page).toHaveURL(/\/performance(\?|$)/);
		await expect
			.poll(() => page.evaluate(() => sessionStorage.getItem('mdt.library_rescue_hold')), { timeout: 30_000 })
			.toBeNull();
		await page.goBack();
		await expect(page.getByTestId('library-workspace')).toBeVisible({ timeout: 30_000 });
		await page.evaluate(() => {
			const seen: string[] = [];
			Reflect.set(window, '__libraryExitKeys', seen);
			const set = Storage.prototype.setItem;
			Storage.prototype.setItem = function (key: string, value: string) {
				if (this === window.sessionStorage) seen.push(key);
				return set.call(this, key, value);
			};
		});
		await page.goForward();
		await expect(page).toHaveURL(/\/performance(\?|$)/);
		const marked = await page.evaluate(() => Reflect.get(window, '__libraryExitKeys') as string[] | undefined);
		expect(marked, 'Forward was a client-side navigation, not a reload').toBeDefined();
		expect(marked).toEqual(expect.arrayContaining(['mdt.library_mode_exit', 'mdt.library_rescue_hold']));
		await expectGigDecks(page, [null, null, null, null]);
	});

	// PREF-01 moved with the page: the inspector's Tempo pref button opens the
	// same editor the old table's BPM right-click did. The engine here is the
	// suite's own throwaway fixture library, but the tempo_pref is still
	// restored in `finally` so a rerun starts from the same state.
	test('tempo pref editor round-trips a range from the inspector', async ({ page }) => {
		await openCrateDesk(page);
		const stableId = await page.locator(ROW).first().getAttribute('data-stable-id');
		if (!stableId) throw new Error('first row is missing data-stable-id');
		const before = await page.request.get(`/api/v1/tracks/${stableId}`);
		expect(before.ok()).toBeTruthy();
		const originalTempoPref = (await before.json()).tempo_pref ?? null;

		await selectRow(page, 0);
		const open = page.getByTestId('inspector-tempo-pref');
		try {
			await open.click();
			const popover = page.locator('.tempo-pref-popover');
			await expect(popover).toBeVisible();
			const regularInput = popover.locator('.tp-regular input');
			const minInput = popover.locator('.tp-minmax').nth(0).locator('input');
			const maxInput = popover.locator('.tp-minmax').nth(1).locator('input');
			await regularInput.fill('140');
			await minInput.fill('138');
			await maxInput.fill('142');
			await popover.getByRole('button', { name: 'Save' }).click();
			await expect(popover).not.toBeVisible();

			await open.click();
			await expect(popover).toBeVisible();
			await expect(regularInput).toHaveValue('140');
			await expect(minInput).toHaveValue('138');
			await expect(maxInput).toHaveValue('142');
			await page.keyboard.press('Escape');
			await expect(popover).not.toBeVisible();
		} finally {
			const fresh = await page.request.get(`/api/v1/tracks/${stableId}`);
			const restore = await page.request.patch(`/api/v1/tracks/${stableId}`, {
				headers: { 'If-Match': fresh.headers()['etag'] },
				data: { tempo_pref: originalTempoPref }
			});
			expect(restore.ok()).toBeTruthy();
		}
	});

	test('tempo pref editor rejects min >= max', async ({ page }) => {
		await openCrateDesk(page);
		await selectRow(page, 0);
		await page.getByTestId('inspector-tempo-pref').click();
		const popover = page.locator('.tempo-pref-popover');
		await expect(popover).toBeVisible();
		await popover.locator('.tp-minmax').nth(0).locator('input').fill('150');
		await popover.locator('.tp-minmax').nth(1).locator('input').fill('140');
		await expect(popover.getByText('min must be less than max')).toBeVisible();
		await expect(popover.getByRole('button', { name: 'Save' })).toBeDisabled();
		await page.keyboard.press('Escape');
	});
});
