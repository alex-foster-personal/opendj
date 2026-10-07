import { test, expect } from '@playwright/test';

import { spendBootLanding } from './support/boot-landing';

for (const path of ['/', '/pairings', '/queues']) {
	test(`smoke: ${path} renders without console errors`, async ({ page }) => {
		// A cold `/` otherwise races PERFMODE-11's one-shot redirect into Gig,
		// which aborts the page's in-flight requests (seen as
		// net::ERR_ABORTED on /api/v1/state/ui-mirror); support/boot-landing.ts
		// has the full account.
		await spendBootLanding(page);
		const errors: string[] = [];
		const failedResponses: string[] = [];
		const failedRequests: string[] = [];
		page.on('console', (msg) => {
			if (msg.type() === 'error') errors.push(msg.text());
		});
		page.on('response', (response) => {
			if (response.status() >= 400) failedResponses.push(`${response.status()} ${response.url()}`);
		});
		page.on('requestfailed', (request) => {
			failedRequests.push(`${request.failure()?.errorText ?? 'unknown'} ${request.url()}`);
		});
		const updateCheck = page.waitForResponse((response) =>
			response.url().includes('/api/v1/update/check')
		);
		await page.goto(path);
		await expect(page.locator('body')).toBeVisible();
		const updateResponse = await updateCheck;
		// A source checkout has no release identity, so the engine deliberately
		// reports UPDATE_CHECK_FAILED as 502. The UI renders that state honestly;
		// it is an expected fixture limitation, not a smoke regression.
		expect([200, 502]).toContain(updateResponse.status());
		if (updateResponse.status() === 502) {
			expect(await updateResponse.json()).toMatchObject({ error: 'update_check_failed' });
		}
		// The USB panel's 503 is a DESIGNED refusal (setup-entry-points.spec.ts
		// has the full account): the browser that `/` mounts since LIBWS-01
		// polls GET /api/v1/usb/volumes, which answers 503 on any host that is
		// not macOS. Pinned to that one endpoint and status, and to exactly as
		// many anonymous 503 console lines as those responses explain.
		const usbRefusals = failedResponses.filter((failure) =>
			/^503 .*\/api\/v1\/usb\/volumes$/.test(failure)
		);
		const unexpectedResponses = failedResponses.filter(
			(failure) => failure !== `502 ${updateResponse.url()}` && !usbRefusals.includes(failure)
		);
		expect(unexpectedResponses).toEqual([]);
		expect(failedRequests).toEqual([]);
		const expectedUpdateConsoleError = updateResponse.status() === 502;
		const USB_503 = 'Failed to load resource: the server responded with a status of 503 (Service Unavailable)';
		let usbConsoleAllowance = usbRefusals.length;
		expect(
			errors.filter((error) => {
				if (error === USB_503 && usbConsoleAllowance > 0) {
					usbConsoleAllowance -= 1;
					return false;
				}
				return (
					!error.includes('favicon') &&
					!(
						expectedUpdateConsoleError &&
						error === 'Failed to load resource: the server responded with a status of 502 (Bad Gateway)'
					)
				);
			})
		).toEqual([]);
	});
}
