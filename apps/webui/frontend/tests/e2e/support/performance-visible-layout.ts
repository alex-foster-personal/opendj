import { expect, type Locator, type Page } from '@playwright/test';

/** FX, LINK, and several top-bar explainers are hidden in the LESS (2-deck) layout. */
export async function ensureMoreDeckLayout(page: Page): Promise<void> {
	const root = page.locator('.perf-root');
	if (await root.evaluate((el) => el.classList.contains('deck-layout-less'))) {
		await page
			.locator('.deck-layout-toggle')
			.getByRole('button', { name: 'MORE', exact: true })
			.click();
		await expect(root).not.toHaveClass(/deck-layout-less/);
	}
}

/** Feedback thumbs sit in a tier that hides below 1530px; performance e2e defaults to 1280. */
export async function ensureWideViewportForTopBarChrome(page: Page): Promise<void> {
	const box = page.viewportSize();
	if (box === null || box.width < 1530) {
		await page.setViewportSize({ width: 2000, height: 1250 });
	}
}

export async function openSettingsOverlay(page: Page): Promise<void> {
	await page.keyboard.press('ControlOrMeta+Comma');
	await expect(page.locator('.so-panel')).toBeVisible();
}

export async function closeSettingsOverlay(page: Page): Promise<void> {
	await page.keyboard.press('Escape');
	await expect(page.locator('.so-panel')).toBeHidden();
}

export async function openAudioIoPanel(page: Page): Promise<void> {
	const trigger = page.getByRole('button', { name: 'SHOW AUDIO I/O' });
	await expect(trigger).toBeVisible();
	await trigger.click();
	await expect(page.getByRole('dialog', { name: 'Audio I/O settings' })).toBeVisible();
}

export async function closeAudioIoPanel(page: Page): Promise<void> {
	await page.keyboard.press('Escape');
	await expect(page.getByRole('dialog', { name: 'Audio I/O settings' })).toBeHidden();
}

/** Visible headphone-cluster MIX knob (not buried in the closed I/O dialog). */
export function visibleHeadphoneMix(page: Page): Locator {
	return page
		.locator('.hp[data-performance-control="headphones"]')
		.getByRole('slider', { name: 'Headphone CUE to MASTER mix' });
}

/** Output-mode label lives only inside the open Audio I/O dialog (CHROME-07). */
export function visibleHeadphoneOutputMode(page: Page): Locator {
	return page.getByRole('dialog', { name: 'Audio I/O settings' }).locator('.hp-mode');
}

export function visibleDeckOne(page: Page): Locator {
	return page.locator('.rb-deck[data-deck-hover="1"]');
}

export function visibleMixerChannelOne(page: Page): Locator {
	return page.locator('.rb-mixer [data-mixer-channel="1"]');
}

export function previewBeatSyncSelect(page: Page): Locator {
	return page.getByLabel('Beat Sync Max: library previews');
}

/** Hover the ControlExplainer wrapper so inert or disabled children still open the pop. */
export function explainerWrap(control: Locator): Locator {
	return control.locator('xpath=ancestor::*[contains(@class,"explainer")][1]');
}

/** Rich explainer popovers portal to `document.body` (`data-explainer-pop`). */
export function explainerPop(page: Page): Locator {
	return page.locator('[data-explainer-pop]');
}

/** Close any open explainer popover so the next hover is not intercepted. */
export async function dismissExplainerChrome(page: Page): Promise<void> {
	await page.mouse.move(0, 0);
	await expect(explainerPop(page)).toHaveCount(0, { timeout: 3000 });
}
