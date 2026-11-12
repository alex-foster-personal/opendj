import { expect, test } from '@playwright/test';

// requirement: SECONDSCREEN-11715
// [if] the library window opens [then] real library rows render without main-window work.
test('second screen renders the library without owning audio or shell orders', async ({ page }) => {
	await page.addInitScript(() => {
		const scope = window as unknown as {
			AudioContext?: typeof AudioContext;
			webkitAudioContext?: typeof AudioContext;
			secondScreenAudioContexts: number;
		};
		scope.secondScreenAudioContexts = 0;
		for (const key of ['AudioContext', 'webkitAudioContext'] as const) {
			const original = scope[key];
			if (original === undefined) continue;
			// Instrument the real constructor; preserve its behavior if called.
			scope[key] = new Proxy(original, {
				construct(target, args, newTarget) {
					scope.secondScreenAudioContexts += 1;
					return Reflect.construct(target, args, newTarget);
				}
			});
		}
	});
	const mainWindowRequests: string[] = [];
	page.on('request', (request) => {
		if (/\/api\/v1\/(shell\/navigate|commands\/next)/.test(request.url())) {
			mainWindowRequests.push(request.url());
		}
	});

	const response = await page.goto('/second-screen');
	expect(response?.status()).toBe(200);
	await expect(page.getByTestId('second-screen')).toBeVisible();
	await page.getByText('All Tracks', { exact: true }).first().click();
	await expect(page.getByTestId('track-row').first()).toBeVisible();
	// Observe the page's real polling loop after rendering, without a mirror publisher.
	const mirror = await page.waitForResponse((r) => r.url().endsWith('/api/v1/state/ui-mirror'));
	expect(mirror.status()).toBe(409);
	await expect(
		page.getByRole('status').filter({ hasText: 'Main window unavailable' })
	).toBeVisible();
	expect(mainWindowRequests).toEqual([]);
	expect(
		await page.evaluate(
			() => (window as unknown as { secondScreenAudioContexts: number }).secondScreenAudioContexts
		)
	).toBe(0);
});
