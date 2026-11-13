import { expect, type Page } from '@playwright/test';

const MOD = process.platform === 'darwin' ? 'Meta' : 'Control';

/** Open CommandBarV2 (cmdk.v2 default-on). It lazy-loads after flags are read. */
export async function openCmdkBar(page: Page): Promise<void> {
	await expect(async () => {
		await page.keyboard.press(`${MOD}+k`);
		await expect(page.locator('#cmdk2-list')).toBeVisible({ timeout: 1_000 });
	}).toPass({ timeout: 30_000 });
	await expect(page.getByTestId('command-bar-input')).toBeFocused();
}

export const CMDK_MOD = MOD;
