/**
 * The engine-unreachable setup screen the packaged desktop app shows.
 *
 * House rule under test: fail fast and visibly. A tester whose engine is
 * not running must get a screen that names the address, the failure and
 * the fix, never a blank window, an endless spinner, or fabricated data.
 *
 * Single-line acceptance checks:
 *
 * - if the page renders the "checking" state forever, the tester sees a
 *   spinner that never resolves -> broken.
 * - if the address tried is not shown verbatim, the tester cannot tell
 *   which port to start the engine on -> broken.
 * - if the failure reason is blank, the screen is decoration -> broken.
 * - if the start command does not carry the port that actually failed,
 *   following the instructions cannot fix the problem -> broken.
 * - if "Check now" does not re-probe, the screen is a dead end -> broken.
 */
import { expect, test } from '@playwright/test';

import { DEAD_ENGINE_ORIGIN, DEAD_ENGINE_PORT } from './playwright.desktop-setup.config';

const HEALTH_URL = `${DEAD_ENGINE_ORIGIN}/api/v1/health`;
const SETUP_URL = `/index.html?engine=${encodeURIComponent(DEAD_ENGINE_ORIGIN)}`;

test.describe('desktop shell setup screen', () => {
	test.beforeAll(async () => {
		// The suite asserts the "engine absent" branch, so prove the engine
		// really is absent. If something is listening on this port the whole
		// run would be testing the wrong path while reporting green.
		let reachable = false;
		try {
			await fetch(HEALTH_URL, { signal: AbortSignal.timeout(2000) });
			reachable = true;
		} catch {
			reachable = false;
		}
		expect(
			reachable,
			`port ${DEAD_ENGINE_PORT} must stay unbound for this suite to mean anything`
		).toBe(false);
	});

	test('names the address, the failure and the fix when the engine is down', async ({
		page
	}) => {
		await page.goto(SETUP_URL);

		const root = page.locator('#root');
		await expect(root).toHaveAttribute('data-state', 'unreachable');

		// The transient state must be gone, not merely covered up.
		await expect(page.locator('#checking-view')).toBeHidden();
		await expect(page.locator('#unreachable-view')).toBeVisible();

		await expect(page.getByRole('heading', { name: 'Open DJ cannot reach its engine' })).toBeVisible();

		// The exact address, so the tester knows which port to start on.
		await expect(page.locator('#probe-url')).toHaveText(HEALTH_URL);

		// A real reason, not an empty box.
		await expect(page.locator('#probe-detail')).not.toHaveText('(unknown)');
		await expect(page.locator('#probe-detail')).toContainText('no response');

		// At least one real attempt happened.
		const attempts = Number(await page.locator('#attempts').innerText());
		expect(attempts).toBeGreaterThanOrEqual(1);

		// The numeric readout carries its hover explanation (house rule).
		await expect(page.locator('#attempts')).toHaveAttribute('title', /how many times/i);

		// The fix has to target the port that actually failed.
		await expect(page.locator('#engine-command')).toContainText(
			`--port ${DEAD_ENGINE_PORT}`
		);
		await expect(page.locator('#engine-command')).toContainText(
			'apps.engine_core serve'
		);

		// No fabricated library anywhere on this screen.
		await expect(page.locator('body')).not.toContainText('No track loaded');
	});

	test('check now re-probes rather than dead-ending', async ({ page }) => {
		await page.goto(SETUP_URL);
		await expect(page.locator('#root')).toHaveAttribute('data-state', 'unreachable');

		const before = Number(await page.locator('#attempts').innerText());
		await page.getByRole('button', { name: 'Check now' }).click();

		await expect
			.poll(async () => Number(await page.locator('#attempts').innerText()), {
				timeout: 10_000
			})
			.toBeGreaterThan(before);

		// Still honest after retrying: the engine is still down.
		await expect(page.locator('#root')).toHaveAttribute('data-state', 'unreachable');
	});
});
