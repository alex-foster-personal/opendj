import { expect, test, type Page } from '@playwright/test';

const PRIORITY_CONTROLS = [
	'button[title^="BeatSyncMax"]',
	'span.ap-wrap > button[title^="AutoPlay"]',
	'button[aria-label="Jobs drawer"]',
	'[role="slider"][aria-label="master volume"]'
];

async function _hitTarget(page: Page, selector: string): Promise<{ hit: boolean; description: string }> {
	return page.locator(selector).evaluate((element) => {
		const rect = element.getBoundingClientRect();
		const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
		return {
			hit: hit === element || element.contains(hit),
			description: hit instanceof Element ? `${hit.tagName}.${hit.className}` : String(hit)
		};
	});
}

test('responsive TopBar keeps priority controls reachable and reports the two-track contract', async ({ page }) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	for (const viewport of [
		{ width: 1864, height: 947 },
		{ width: 1280, height: 800 },
		{ width: 1024, height: 800 },
		{ width: 820, height: 800 }
	]) {
		await page.setViewportSize(viewport);
		for (const selector of PRIORITY_CONTROLS) {
			await expect(page.locator(selector)).toBeVisible();
			await expect
				.poll(async () => (await _hitTarget(page, selector)).hit, {
					message: `${viewport.width}px ${selector} did not settle on its own hit target`
				})
				.toBe(true);
		}
		if (viewport.width > 820) {
			await expect(page.locator('input[aria-label="text command entry"]')).toBeVisible();
			await expect
				.poll(async () => (await _hitTarget(page, 'input[aria-label="text command entry"]')).hit, {
					message: `${viewport.width}px command entry did not settle on its own hit target`
				})
				.toBe(true);
		}
	}

	await page.locator('span.ap-wrap > button[title^="AutoPlay"]').focus();
	await expect(page.locator('.ap-menu')).toBeVisible();
	await expect(page.locator('button.ap-two-track')).toBeDisabled();
	await expect(page.locator('button.ap-two-track')).toHaveAttribute('title', /Not built yet/);

	const refusal = await page.evaluate(async () => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		try {
			await ipc.dispatch({ type: 'auto_play_two_track' });
			return null;
		} catch (error) {
			return error instanceof Error ? error.message : String(error);
		}
	});
	expect(refusal).toMatch(/not_implemented/);
});
