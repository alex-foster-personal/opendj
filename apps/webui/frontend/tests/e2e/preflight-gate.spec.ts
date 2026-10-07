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

import { expectFirstRunSetup } from './support/expect-first-run-setup';
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

		// The real app is what's on screen instead. "/" is now Library mode's
		// full-bleed Crate Desk (LIBWS-01), with no app-shell heading or sidebar
		// to look for, and a v1 cold open moves on to Gig (PERFMODE-11, as
		// gig-boot-landing.spec.ts asserts). So wait for the settled app: Gig's
		// route with its performance API up, and still no gate.
		await expect(page).toHaveURL(/\/performance/, { timeout: 30_000 });
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
		await expect(page.locator('[data-preflight-mode="boot"]')).toHaveCount(0);
	});

	test('a library with nothing imported auto-opens setup instead of trapping the user', async ({
		page
	}) => {
		await page.goto(`${PREFLIGHT_GATE_BROKEN_ORIGIN}/`);

		// P0-1 (#2722): the setup wizard owns the empty-library ask; the boot
		// gate must not be the only interactive surface.
		await expectFirstRunSetup(page);

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
		// The engine's own copy (apps/webui/server/preflight_checks.py, 1f97fe0dd).
		expect(libraryRow?.user_detail).toContain('No tracks in your library yet.');

		// A fresh install has nothing recorded to sample, which is an honest
		// `pending`, never a fabricated pass (this issue's own denominator rule).
		const audioRow = body.checks.find(
			(check: { id: string }) => check.id === 'audio-access'
		);
		expect(audioRow?.status).toBe('pending');
	});
});
