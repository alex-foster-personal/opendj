/**
 * PREFLIGHT-03 / issue #2722: a fresh engine data dir auto-opens the Setup
 * wizard within one health-probe cycle, without manual intervention.
 */
import { expect, test } from '@playwright/test';

import { expectFirstRunSetup } from './support/expect-first-run-setup';
import { PREFLIGHT_GATE_BROKEN_ORIGIN } from './playwright.preflight-gate.config';

test.describe('fresh install onboarding', () => {
	test('empty library auto-opens the setup wizard', async ({ page }) => {
		const setupStatusRequests: string[] = [];
		page.on('request', (request) => {
			if (request.url().includes('/api/v1/setup/status')) {
				setupStatusRequests.push(request.url());
			}
		});

		await page.goto(`${PREFLIGHT_GATE_BROKEN_ORIGIN}/`);

		await expectFirstRunSetup(page);

		expect(setupStatusRequests.length).toBeGreaterThan(0);
	});
});
