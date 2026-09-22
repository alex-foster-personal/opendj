import { expect, test } from '@playwright/test';

test('Enter respects a directly focused deck target and playlist row', async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const row = page.getByTestId('track-row').first();
	await expect(row).toBeVisible();
	await row.click();
	const stableId = await row.getAttribute('data-stable-id');
	const loadsBefore = await page.evaluate(() =>
		window.musicDjToolsPerformance!.query().history.filter((command) => command.type === 'load').length
	);
	const deckTarget = row.locator('button.deck-target').first();
	await deckTarget.focus();
	await expect(deckTarget).toBeFocused();
	await page.keyboard.press('Enter');
	await expect.poll(() => page.evaluate(() =>
		window.musicDjToolsPerformance!.query().history.filter((command) => command.type === 'load').length
	)).toBe(loadsBefore + 1);
	await expect.poll(() => page.evaluate(() => window.musicDjToolsPerformance!.query().decks[1].stable_id)).toBe(stableId);

	const playlist = page.getByTestId('playlist-row').first();
	await expect(playlist).toBeVisible();
	await playlist.focus();
	const loadsAtPlaylist = await page.evaluate(() =>
		window.musicDjToolsPerformance!.query().history.filter((command) => command.type === 'load').length
	);
	await page.keyboard.press('Enter');
	await page.keyboard.press('Enter');
	await expect(playlist).toBeFocused();
	await expect.poll(() => page.evaluate(() =>
		window.musicDjToolsPerformance!.query().history.filter((command) => command.type === 'load').length
	)).toBe(loadsAtPlaylist);
});

// IOPIN-01: exercise real keyboard/DOM focus and production browse adapter.
test('browser selection scrolls, MIDI reclaims tracks, and sliders keep arrows', async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const rows = page.getByTestId('track-row');
	await expect(rows.first()).toBeVisible();
	await rows.first().click();
	for (let i = 0; i < 25; i++) await page.keyboard.press('ArrowDown');
	const selected = page.locator('[data-testid="track-row"].rb-row-selected');
	await expect(selected).toBeVisible();
	const before = await selected.getAttribute('data-stable-id');
	const slider = page.getByRole('slider', { name: 'master volume', exact: true });
	await slider.focus();
	await page.keyboard.press('ArrowLeft');
	await expect(slider).toBeFocused();
	await expect(selected).toHaveAttribute('data-stable-id', before!);
	await page.evaluate(async () => {
		const url = '/src/lib/rb/midi/browse-adapter.ts';
		const { getBrowseAdapter } = await import(url);
		getBrowseAdapter().moveSelection(1);
	});
	await expect(selected).not.toHaveAttribute('data-stable-id', before!);
	await expect(selected).toBeVisible();
	// A/D are the horizontal counterparts of W/S, not silently ignored.
	await selected.focus();
	await page.keyboard.press('d');
	await expect(selected.locator('button.deck-target').first()).toBeFocused();
	// Open a real row menu: neither keyboard nor MIDI can change its selection.
	await selected.click({ button: 'right' });
	await expect(page.getByTestId('context-menu')).toBeVisible();
	const menuSelection = await selected.getAttribute('data-stable-id');
	await page.evaluate(async () => {
		const url = '/src/lib/rb/midi/browse-adapter.ts';
		const { getBrowseAdapter } = await import(url);
		getBrowseAdapter().moveSelection(1);
	});
	await expect(selected).toHaveAttribute('data-stable-id', menuSelection!);
	await page.keyboard.press('Escape');
	await expect(page.getByTestId('context-menu')).toBeHidden();
	// Ordinary buttons own Enter; rapid presses must never double-load a row.
	const masterMute = page.getByRole('button', { name: 'master mute', exact: true });
	await masterMute.focus();
	await page.keyboard.press('Enter');
	await page.keyboard.press('Enter');
	await expect(masterMute).toBeFocused();
	expect(await page.evaluate(() => window.musicDjToolsPerformance!.query().decks[1].stable_id)).toBeNull();
});

test('BPM borders follow a real decoded master instead of permanent column tint', async ({ page }) => {
	const stableId = process.env.PERFORMANCE_E2E_MIXTOUR_TRACK;
	if (!stableId) throw new Error('Set PERFORMANCE_E2E_MIXTOUR_TRACK to an analyzed real library track');
	await page.goto('/performance?muted=1');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.evaluate(async (stable_id) => {
		const ipc = window.musicDjToolsPerformance!;
		await ipc.dispatch({ type: 'load', deck: 1, stable_id });
		await ipc.dispatch({ type: 'master', deck: 1 });
	}, stableId);
	await expect.poll(() => page.evaluate(() => window.musicDjToolsPerformance!.query().decks[1].bpm)).toBeGreaterThan(0);
	const cells = page.locator('td.c-bpm.bpm-compatible');
	await expect(cells.first()).toBeVisible();
	const data = await page.evaluate(() => {
		const state = window.musicDjToolsPerformance!.query();
		const master = state.decks[1].effective_bpm!;
		return Array.from(document.querySelectorAll('td.c-bpm')).map((cell) => ({
			bpm: Number(cell.textContent?.trim()), compatible: cell.classList.contains('bpm-compatible'),
			delta: Math.min(...[master, master / 2, master * 2].map((ref) => Math.abs(Number(cell.textContent?.trim()) - ref)))
		}));
	});
	expect(data.length).toBeGreaterThan(3);
	for (const cell of data.filter((cell) => cell.bpm > 0)) expect(cell.compatible).toBe(cell.delta < 8);
	await page.evaluate(() => window.musicDjToolsPerformance!.dispatch({ type: 'unload', deck: 1 }));
});
