/**
 * requirement: CSUI-01
 * if CloudSync settings doors or overlay links stop navigating then broken
 */
import { test, expect, type Page } from '@playwright/test';

const SETTINGS_CHORD = process.platform === 'darwin' ? 'Meta+Comma' : 'Control+Comma';

async function gotoShellReady(page: Page, path: string): Promise<void> {
	await page.goto(path);
	await page.waitForFunction(
		() => typeof (window as unknown as { __mdtPerfLog?: unknown }).__mdtPerfLog === 'function',
		undefined,
		{ timeout: 30_000 }
	);
}

test.describe('CloudSync settings navigation doors', () => {
	test('/settings links open the matching /cloudsync tab', async ({ page }) => {
		await gotoShellReady(page, '/settings');

		const machinesLink = page.getByRole('link', {
			name: 'CloudSync: machines & asset policy'
		});
		const pinsLink = page.getByRole('link', { name: 'CloudSync: playlist pins' });
		const overviewLink = page.getByRole('link', { name: 'CloudSync: fleet overview' });

		await expect(machinesLink).toBeVisible();
		await expect(pinsLink).toBeVisible();
		await expect(overviewLink).toBeVisible();

		await page.screenshot({
			path: 'test-results/cloudsync-settings-doors.png',
			fullPage: true
		});

		await machinesLink.click();
		await expect(page).toHaveURL(/\/cloudsync\?tab=policies/);
		await expect(page.getByRole('tab', { name: 'Machines & policies' })).toHaveAttribute(
			'aria-selected',
			'true'
		);

		await gotoShellReady(page, '/settings');
		await pinsLink.click();
		await expect(page).toHaveURL(/\/cloudsync\?tab=pins/);
		await expect(page.getByRole('tab', { name: 'Playlist pins' })).toHaveAttribute(
			'aria-selected',
			'true'
		);

		await gotoShellReady(page, '/settings');
		await overviewLink.click();
		await expect(page).toHaveURL(/\/cloudsync\?tab=overview/);
		await expect(page.getByRole('tab', { name: 'Hydration overview' })).toHaveAttribute(
			'aria-selected',
			'true'
		);
	});

	test('Cmd+, overlay CloudSync links navigate and close', async ({ page }) => {
		await gotoShellReady(page, '/');
		await page.keyboard.press(SETTINGS_CHORD);
		await expect(page.getByRole('dialog', { name: 'Settings' })).toBeVisible();

		await page.getByRole('button', { name: 'CloudSync', exact: true }).click();
		await page.getByRole('option', { name: /CloudSync: machines & asset policy/ }).click();
		await expect(page.getByRole('dialog', { name: 'Settings' })).not.toBeVisible();
		await expect(page).toHaveURL(/\/cloudsync\?tab=policies/);
		await expect(page.getByRole('tab', { name: 'Machines & policies' })).toHaveAttribute(
			'aria-selected',
			'true'
		);

		await gotoShellReady(page, '/');
		await page.keyboard.press(SETTINGS_CHORD);
		await expect(page.getByRole('dialog', { name: 'Settings' })).toBeVisible();
		await page.getByRole('button', { name: 'CloudSync', exact: true }).click();
		const pinsRow = page.getByRole('option', { name: /CloudSync: playlist pins/ });
		await pinsRow.focus();
		await page.keyboard.press('Enter');
		await expect(page.getByRole('dialog', { name: 'Settings' })).not.toBeVisible();
		await expect(page).toHaveURL(/\/cloudsync\?tab=pins/);
		await expect(page.getByRole('tab', { name: 'Playlist pins' })).toHaveAttribute(
			'aria-selected',
			'true'
		);
	});
});
