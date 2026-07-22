import { expect, test } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const SCREENSHOT_DIR = join(REPOSITORY_ROOT, '.tmp');

test('real event-store analytics render and filter through the HTTP contract', async ({ page }) => {
	const consoleErrors: string[] = [];
	const failedResources: string[] = [];
	page.on('console', (message) => {
		if (message.type() === 'error') consoleErrors.push(message.text());
	});
	page.on('response', (response) => {
		if (response.status() >= 400) failedResources.push(response.url());
	});

	const initialResponse = page.waitForResponse(
		(response) => response.url().includes('/api/play-analytics?') && response.status() === 200
	);
	await page.goto('/play-analytics');
	await initialResponse;

	await expect(page.getByRole('heading', { name: 'Play analytics' })).toBeVisible();
	await expect(page.getByLabel('Play summary')).toContainText(
		/2\s*sessions\s*4\s*plays\s*3\s*unique tracks/
	);
	await expect(page.getByRole('heading', { name: 'Recent sessions' })).toBeVisible();
	await expect(page.getByText('warehouse-2026-07-21')).toBeVisible();
	await expect(page.getByRole('heading', { name: 'Most played' })).toBeVisible();
	await expect(page.getByText('Alpha', { exact: true })).toBeVisible();

	mkdirSync(SCREENSHOT_DIR, { recursive: true });
	await page.screenshot({ path: join(SCREENSHOT_DIR, 'play-analytics-1280x800.png') });

	const filteredResponse = page.waitForResponse(
		(response) =>
			response.url().includes('share_state=shared_local') && response.status() === 200
	);
	await page.getByLabel('Session visibility').selectOption('shared_local');
	await filteredResponse;
	await expect(page.getByLabel('Play summary')).toContainText(
		/1\s*sessions\s*2\s*plays\s*2\s*unique tracks/
	);
	await expect(page.getByText('warehouse-2026-07-21')).not.toBeVisible();
	await expect(page.getByText('studio-2026-07-20')).toBeVisible();
	expect(failedResources.every((url) => url.endsWith('/favicon.svg'))).toBe(true);
	expect(
		consoleErrors.filter((error) => !error.startsWith('Failed to load resource:'))
	).toEqual([]);
});
