/**
 * Real-browser coverage for library context menus. The root Playwright config
 * starts a real fixture engine and Vite, so these assertions exercise the
 * production menu wiring without intercepted responses or fabricated state.
 *
 * Acceptance:
 * - [if] right-clicking a track, playlist, or folder shows no menu [then stop]
 * - [if] Shift+F10 cannot open the track menu [then stop]
 * - [if] Escape leaves the menu visible [then stop]
 */
import { expect, test } from '@playwright/test';

const MENU = '[data-testid="context-menu"]';

test('track, playlist, and folder context menus are pointer and keyboard reachable', async ({ page }) => {
	// pin 246b0f5 FIX ROUND 2 note (now reverted, FIX ROUND 3): round 2
	// pinned this spec's viewport to 1280x800 because round 1's MORE
	// deck-area floor growth (497px -> 524px) collapsed the already-tight
	// 720px library row until its rows and this route's own .bottom-bar/
	// deck-area stems overlapped in the hit-test order, blocking the clicks
	// below. Sol's round-3 review correctly called that a test-side dodge
	// of a real layout regression rather than a fix. Round 3 restored the
	// MORE floor to its original 497px by compacting ChannelStrip.svelte's
	// margins instead (see +page.svelte's perf-root comment), which is
	// exactly the floor this spec already passed against on `main` before
	// pin 246b0f5 ever touched it - so the viewport override is removed
	// and this now runs at the root suite's native (unset -> Chrome's
	// 1280x720 default) viewport again, the strongest available proof the
	// regression is actually fixed rather than hidden.
	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });

	const track = page.locator('[data-testid="track-row"]').first();
	await track.click();
	await track.click({ button: 'right' });
	await expect(page.locator(MENU)).toContainText('Load to deck 1');
	await expect(page.locator('.qd')).toHaveCount(0);
	await expect(page.getByRole('menuitem', { name: 'Mark offline' })).toHaveAttribute('title', 'not implemented - see PARITY-TODO');
	await page.mouse.click(1, 1);
	await expect(page.locator(MENU)).toHaveCount(0);

	await track.click({ button: 'right' });
	await expect(page.locator(MENU)).toContainText('Load to deck 1');
	await page.keyboard.press('Escape');
	await expect(page.locator(MENU)).toHaveCount(0);

	await track.focus();
	await page.keyboard.press('Shift+F10');
	await expect(page.locator(MENU)).toContainText('Load to deck 1');
	await page.keyboard.press('Escape');

	const playlist = page.locator('[data-testid="playlist-row"]').first();
	await expect(playlist).toBeVisible();
	await playlist.click({ button: 'right' });
	await expect(page.locator(MENU)).toContainText('New playlist');
	await expect(page.getByRole('menuitem', { name: 'Duplicate' })).toBeEnabled();
	await page.keyboard.press('Escape');

	const folder = page.locator('[data-testid="playlist-folder"]');
	await folder.click({ button: 'right' });
	await expect(page.locator(MENU)).toContainText('New folder');
	const folderDuplicate = page.getByRole('menuitem', { name: 'Duplicate' });
	if (await folderDuplicate.count()) {
		await expect(folderDuplicate).toBeDisabled();
	}
});
