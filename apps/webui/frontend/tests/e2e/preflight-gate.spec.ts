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
	test('a healthy library lands straight in the app, no gate shown', async ({ page }) => {
		await page.goto(`${PREFLIGHT_GATE_HEALTHY_ORIGIN}/`);

		// The gate never renders at all: every check passed before the first
		// paint the test can observe, matching the "well under 300ms" ask.
		await expect(page.locator('[data-preflight-mode="boot"]')).toHaveCount(0);

		// The real app shell is what's on screen instead.
		await expect(page.getByRole('heading', { name: 'Open DJ' })).toBeVisible();
		await expect(page.getByRole('link', { name: 'Library' })).toBeVisible();
	});

	test('a library with nothing imported holds the gate on library-attached', async ({
		page
	}) => {
		await page.goto(`${PREFLIGHT_GATE_BROKEN_ORIGIN}/`);

		const gate = page.locator('[data-preflight-mode="boot"]');
		await expect(gate).toBeVisible();

		// No skip/continue-anyway control exists at all -- there is nothing to
		// query for, so the negative is that the app shell never appears.
		await expect(page.getByRole('link', { name: 'Library' })).toHaveCount(0);

		const libraryRow = gate.locator('[data-check-id="library-attached"]');
		await expect(libraryRow).toHaveAttribute('data-check-status', 'fail');
		await expect(libraryRow).toContainText('0 tracks');

		// Import path must be offered on the boot gate (issue #2722).
		await expect(gate.getByTestId('preflight-import-music')).toBeVisible();
		await expect(gate.getByTestId('preflight-run-setup')).toBeVisible();

		// A fresh install has nothing recorded to sample, which is an honest
		// `pending`, never a fabricated pass (this issue's own denominator rule).
		const audioRow = gate.locator('[data-check-id="audio-access"]');
		await expect(audioRow).toHaveAttribute('data-check-status', 'pending');
	});
});
