import { expect, test } from '@playwright/test';

const SEED_TEXT = 'comment pins re-opened later must close on a real column resize (seeded by performance-feedback-card-dismiss.spec.ts)';

test('reopened comment card closes on a real column resize pointerdown', async ({ page }) => {
	// Provision the pin through the production API (the dev server proxies /api to the engine),
	// so the scenario does not depend on whatever the live feedback store happens to hold.
	const seeded = await page.request.post('/api/v1/feedback/comments', {
		data: { page: '/performance', text: SEED_TEXT, x_pct: 55, y_pct: 45 }
	});
	if (!seeded.ok()) throw new Error(`seeding the feedback pin failed: HTTP ${seeded.status()}`);
	const seededId = (await seeded.json()).id as string;
	try {
		await runScenario(page);
	} finally {
		const archived = await page.request.post(`/api/v1/feedback/comments/${seededId}/archive`);
		if (!archived.ok()) throw new Error(`archiving the seeded pin ${seededId} failed: HTTP ${archived.status()}`);
	}
});

async function runScenario(page: import('@playwright/test').Page): Promise<void> {
	// Pin 88e3abec02a0 defaults "show feedback comment pins" OFF for a new
	// viewer (per-viewer localStorage). This spec is exercising the pins
	// themselves, so opt this viewer in before the widget mounts and reads it.
	await page.addInitScript(() => {
		window.localStorage.setItem('mdt.feedback.pinsVisible.v1', '1');
	});
	await page.goto('/performance', { waitUntil: 'domcontentloaded' });
	const existingPin = page.locator(`button.fb-pin[title^="${SEED_TEXT}"]`);
	await expect(existingPin).toBeVisible({ timeout: 60_000 });
	await existingPin.click();
	const card = page.getByRole('dialog', { name: 'Comment pin', exact: true });
	await expect(card).toBeVisible();
	await card.locator('.fb-body-text').click();
	await expect(card).toBeVisible();

	// Real resize handles stop propagation. Find an unobstructed handle rather
	// than dispatching a synthetic event or forcing a click through the card.
	const point = await page.locator('.col-resize').evaluateAll((handles) => {
		for (const handle of handles) {
			const rect = handle.getBoundingClientRect();
			const x = rect.x + rect.width / 2;
			const y = rect.y + rect.height / 2;
			if (rect.width > 0 && rect.height > 0 && handle.contains(document.elementFromPoint(x, y))) {
				return { x, y };
			}
		}
		return null;
	});
	if (point === null) throw new Error('No unobstructed real column resize handle is available');
	await page.mouse.move(point.x, point.y);
	await page.mouse.down();
	await page.mouse.up();
	await expect(card).toHaveCount(0);
	await existingPin.click();
	await expect(card).toBeVisible();
	await page.getByRole('button', { name: 'Close comment pin', exact: true }).click();
	await expect(card).toHaveCount(0);
}
