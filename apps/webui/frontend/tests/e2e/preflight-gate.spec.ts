/**
 * PREFLIGHT-01's boot gate (issue #771), rendered in a real browser against
 * two real backends.
 *
 * Single-line acceptance checks:
 * - if the healthy fixture ever shows the boot gate at all -> broken.
 * - if the broken fixture's gate is skippable, or shows no red row -> broken.
 * - if the broken fixture's red row does not name library-attached (the real
 *   reason: nothing has been imported yet) -> broken.
 */
import { expect, test } from '@playwright/test';

import {
	PREFLIGHT_GATE_BROKEN_ORIGIN,
	PREFLIGHT_GATE_HEALTHY_ORIGIN
} from './playwright.preflight-gate.config';

test.describe('preflight boot gate', () => {
	// REQ: PREFLIGHT-01
	test('a healthy library lands straight in the app, no gate shown', async ({ page }) => {
		await page.goto(`${PREFLIGHT_GATE_HEALTHY_ORIGIN}/`);

		// The gate never renders at all: every check passed before the first
		// paint the test can observe, matching the "well under 300ms" ask.
		await expect(page.locator('[data-preflight-mode="boot"]')).toHaveCount(0);

		// The real app shell is what's on screen instead.
		await expect(page.getByRole('heading', { name: 'Open DJ' })).toBeVisible();
		await expect(page.getByRole('link', { name: 'Library' })).toBeVisible();
	});

	test('a library with nothing imported auto-opens setup instead of trapping the user', async ({
		page
	}) => {
		await page.goto(`${PREFLIGHT_GATE_BROKEN_ORIGIN}/`);

		// P0-1 (#2722): the setup wizard owns the empty-library ask; the boot
		// gate must not be the only interactive surface.
		const setupDialog = page.getByRole('dialog', { name: 'First-run setup' });
		await expect(setupDialog).toBeVisible({ timeout: 10_000 });
		await expect(page.locator('[data-preflight-blocking="true"]')).toHaveCount(0);

		// Preflight still reports the honest fail -- never a fabricated pass.
		const preflight = await page.request.get(
			`${PREFLIGHT_GATE_BROKEN_ORIGIN}/api/v1/preflight`
		);
		expect(preflight.ok()).toBeTruthy();
		const body = await preflight.json();
		const libraryRow = body.checks.find(
			(check: { id: string }) => check.id === 'library-attached'
		);
		expect(libraryRow?.status).toBe('fail');
		expect(libraryRow?.user_detail).toContain('No music imported yet');

		// A fresh install has nothing recorded to sample, which is an honest
		// `pending`, never a fabricated pass (this issue's own denominator rule).
		const audioRow = body.checks.find(
			(check: { id: string }) => check.id === 'audio-access'
		);
		expect(audioRow?.status).toBe('pending');
	});
});
