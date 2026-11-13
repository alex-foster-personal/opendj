import type { Page } from '@playwright/test';

/** Pin 200% browser zoom via Chromium DevTools (issue #12949). */
export async function setBrowserZoom(page: Page, scale: number): Promise<void> {
	if (!Number.isFinite(scale) || scale <= 0) {
		throw new Error(`browser zoom scale must be a positive number, got ${scale}`);
	}
	const client = await page.context().newCDPSession(page);
	await client.send('Emulation.setPageScaleFactor', { pageScaleFactor: scale });
}
