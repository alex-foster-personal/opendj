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
	await page.keyboard.press('Escape');

	const folder = page.locator('[data-testid="playlist-folder"]');
	await folder.click({ button: 'right' });
	await expect(page.locator(MENU)).toContainText('New folder');
});
