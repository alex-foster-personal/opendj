import { expect, test, type Locator, type Page } from '@playwright/test';

const SEED_TEXT =
	'comment pins re-opened later must close on a real column resize (seeded by performance-feedback-card-dismiss.spec.ts)';

const REPLY_SEED_TEXT =
	'comment pins can take a follow-up on the same marker (seeded by performance-feedback-card-dismiss.spec.ts)';

const COMMENT_PIN_DIALOG = 'Comment pin';
const FOLLOW_UP_FIELD = 'Follow-up comment';
const ADD_FOLLOW_UP_BUTTON = 'Add follow-up comment';

function commentPinDialog(page: Page): Locator {
	return page.getByRole('dialog', { name: COMMENT_PIN_DIALOG, exact: true });
}

function followUpFieldInCommentPin(card: Locator): Locator {
	return card.getByRole('textbox', { name: FOLLOW_UP_FIELD, exact: true });
}

function addFollowUpButtonInCommentPin(card: Locator): Locator {
	return card.getByRole('button', { name: ADD_FOLLOW_UP_BUTTON, exact: true });
}

test('reopened comment card accepts a follow-up that survives reload', async ({ page }) => {
	const seeded = await page.request.post('/api/v1/feedback/comments', {
		data: {
			page: '/performance',
			text: REPLY_SEED_TEXT,
			x_pct: 55,
			y_pct: 45,
			ui: 'chrome-loop',
			viewport_width: 1280,
			viewport_height: 800
		}
	});
	if (!seeded.ok()) throw new Error(`seeding the feedback pin failed: HTTP ${seeded.status()}`);
	const seededId = (await seeded.json()).id as string;
	const patched = await page.request.patch(`/api/v1/feedback/comments/${seededId}`, {
		data: { agent_note: 'queued as #905' }
	});
	if (!patched.ok()) throw new Error(`patching agent_note failed: HTTP ${patched.status()}`);
	const followUp = 'still broken after the first fix';
	try {
		await page.addInitScript(() => {
			window.localStorage.setItem('mdt.feedback.pinsVisible.v1', '1');
		});
		await page.goto('/performance', { waitUntil: 'domcontentloaded' });
		const existingPin = page.locator(`button.fb-pin[title^="${REPLY_SEED_TEXT}"]`);
		await expect(existingPin).toBeVisible({ timeout: 60_000 });
		await existingPin.click();
		const card = commentPinDialog(page);
		await expect(card).toBeVisible();
		await expect(card.locator('.fb-body-text')).toHaveText(REPLY_SEED_TEXT);
		await expect(card.locator('.fb-note')).toContainText('queued as #905');
		await followUpFieldInCommentPin(card).fill(followUp);
		await addFollowUpButtonInCommentPin(card).click();
		await expect(card.locator('.fb-note').filter({ hasText: followUp })).toHaveCount(1);
		const listed = await page.request.get('/api/v1/feedback/comments');
		const pins = (await listed.json()).comments as Array<{
			id: string;
			replies?: Array<{ text: string }>;
		}>;
		expect(pins).toHaveLength(1);
		expect(pins[0].replies?.some((r) => r.text === followUp)).toBe(true);
		await page.reload({ waitUntil: 'domcontentloaded' });
		await existingPin.click();
		await expect(card.locator('.fb-note').filter({ hasText: followUp })).toHaveCount(1);
	} finally {
		const fixed = await page.request.patch(`/api/v1/feedback/comments/${seededId}`, {
			data: { status: 'fixed' }
		});
		if (!fixed.ok())
			throw new Error(`marking the seeded pin ${seededId} fixed failed: HTTP ${fixed.status()}`);
		const archived = await page.request.post(`/api/v1/feedback/comments/${seededId}/archive`);
		if (!archived.ok())
			throw new Error(`archiving the seeded pin ${seededId} failed: HTTP ${archived.status()}`);
	}
});

test('reopened comment card closes on a real column resize pointerdown', async ({ page }) => {
	// Provision the pin through the production API (the dev server proxies /api to the engine),
	// so the scenario does not depend on whatever the live feedback store happens to hold.
	const seeded = await page.request.post('/api/v1/feedback/comments', {
		data: {
			page: '/performance',
			text: SEED_TEXT,
			x_pct: 55,
			y_pct: 45,
			ui: 'chrome-loop',
			viewport_width: 1280,
			viewport_height: 800
		}
	});
	if (!seeded.ok()) throw new Error(`seeding the feedback pin failed: HTTP ${seeded.status()}`);
	const seededId = (await seeded.json()).id as string;
	try {
		await runScenario(page);
	} finally {
		// Archive requires the pin to be fixed/merged first (409 PIN_NOT_DONE
		// otherwise, apps/webui/server/routes/feedback_pins.py:archive_comment) -
		// this pin is only ever seeded `open`, so transition it before archiving.
		const patched = await page.request.patch(`/api/v1/feedback/comments/${seededId}`, {
			data: { status: 'fixed' }
		});
		if (!patched.ok())
			throw new Error(`marking the seeded pin ${seededId} fixed failed: HTTP ${patched.status()}`);
		const archived = await page.request.post(`/api/v1/feedback/comments/${seededId}/archive`);
		if (!archived.ok())
			throw new Error(`archiving the seeded pin ${seededId} failed: HTTP ${archived.status()}`);
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
	// Scroll the handle into view first (a real scroll a user would perform): on
	// the 1280x800 project viewport the table's handle centres sit at y=801.75,
	// 1.75 px below the bottom edge, so elementFromPoint returns null for an
	// offscreen centre even though the rect ORIGIN is inside the viewport.
	const handlesLocator = page.locator('.col-resize');
	await handlesLocator.first().scrollIntoViewIfNeeded();
	const point = await handlesLocator.evaluateAll((handles) => {
		const viewportHeight = window.innerHeight;
		const viewportWidth = window.innerWidth;
		for (const [index, handle] of handles.entries()) {
			const rect = handle.getBoundingClientRect();
			const x = rect.x + rect.width / 2;
			const y = rect.y + rect.height / 2;
			const centreOnScreen = x >= 0 && x <= viewportWidth && y >= 0 && y <= viewportHeight;
			if (
				rect.width > 0 &&
				rect.height > 0 &&
				centreOnScreen &&
				handle.contains(document.elementFromPoint(x, y))
			) {
				return {
					x,
					y,
					index,
					columnWidth: (handle.parentElement as HTMLElement).getBoundingClientRect().width
				};
			}
		}
		return null;
	});
	if (point === null) throw new Error('No unobstructed real column resize handle is available');
	// A dismissal alone would pass even if the handle were inert, so drag the real
	// handle and require the owning column to widen: that is the "resizing still
	// works" half of the acceptance, measured on the same physical pointer path.
	await page.mouse.move(point.x, point.y);
	await page.mouse.down();
	await page.mouse.move(point.x + 40, point.y, { steps: 5 });
	await page.mouse.up();
	const widened = await handlesLocator.nth(point.index).evaluate((handle) => {
		return (handle.parentElement as HTMLElement).getBoundingClientRect().width;
	});
	expect(widened - point.columnWidth).toBeGreaterThan(20);
	await expect(card).toHaveCount(0);
	await existingPin.click();
	await expect(card).toBeVisible();
	await page.getByRole('button', { name: 'Close comment pin', exact: true }).click();
	await expect(card).toHaveCount(0);
}
