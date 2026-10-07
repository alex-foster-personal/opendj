// requirement: LIBWS-01
// requirement: LIBWS-02
// requirement: LIBWS-03
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

	test('a deck load is refused without building an audio engine', async ({ page }) => {
		await openCrateDesk(page);
		await selectRow(page, 0);
		const row = page.locator(ROW).first();
		await row.locator('td.c-title').dblclick();
		await expect(page.locator('.toast-headline').filter({ hasText: 'Library mode has no decks' })).toBeVisible();

		await row.locator('td.c-title').click({ button: 'right' });
		await expect(page.locator('[data-testid^="context-menu-load-deck-"]')).toHaveCount(0);
		await page.keyboard.press('Escape');

		const probe = await page.evaluate(async () => {
			const mod = await import(new URL('/src/lib/rb/library-mode-runtime.ts', location.href).href);
			return mod.readLibraryModeIdleProbe();
		});
		expect(probe.audio_context_count).toBe(0);
		expect(probe.deck_nodes_present).toBe(false);
		expect(probe.prefetch_ready_count).toBe(0);
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
