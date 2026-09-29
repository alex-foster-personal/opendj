import { expect, test } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const SCREENSHOT_DIR = join(REPOSITORY_ROOT, '.tmp');

type WheelAxis = {
	key: string;
	label: string;
	enabled: boolean;
	reason: string | null;
};

type WheelPayload = {
	total_tracks: number;
	unclassified_track_count: number;
	families: unknown[];
	axes: WheelAxis[];
};

function isWheelPayload(value: unknown): value is WheelPayload {
	if (typeof value !== 'object' || value === null) return false;
	const row = value as Record<string, unknown>;
	return (
		typeof row.total_tracks === 'number' &&
		typeof row.unclassified_track_count === 'number' &&
		Array.isArray(row.families) &&
		Array.isArray(row.axes)
	);
}

test('real library wheel renders genre families through the HTTP contract', async ({ page }) => {
	const consoleErrors: string[] = [];
	const failedResources: { url: string; status: number }[] = [];
	page.on('console', (message) => {
		if (message.type() === 'error') consoleErrors.push(message.text());
	});
	page.on('response', (response) => {
		if (response.status() < 400) return;
		failedResources.push({ url: response.url(), status: response.status() });
	});

	const initialResponse = page.waitForResponse(
		(response) => response.url().includes('/api/v1/library/wheel') && response.status() === 200
	);
	const updateCheck = page.waitForResponse((response) =>
		response.url().includes('/api/v1/update/check')
	);
	await page.goto('/library-wheel');
	const wheelResponse = await initialResponse;
	const payload = await wheelResponse.json();
	expect(isWheelPayload(payload)).toBe(true);
	if (!isWheelPayload(payload)) return;

	await expect(page.getByRole('heading', { name: 'Library wheel' })).toBeVisible();
	await expect(page.getByRole('link', { name: 'Library wheel' })).toBeVisible();

	const summary = page.getByLabel('Library summary');
	await expect(summary).toContainText(String(payload.total_tracks));
	await expect(summary).toContainText(String(payload.families.length));
	await expect(summary).toContainText(String(payload.unclassified_track_count));
	await expect(summary).toContainText(
		new RegExp(
			`${payload.total_tracks}\\s*tracks\\s*${payload.families.length}\\s*families\\s*${payload.unclassified_track_count}\\s*unclassified`
		)
	);

	await expect(page.getByRole('img', { name: 'Radial library explorer' })).toBeVisible();
	expect(await page.locator('svg path').count()).toBeGreaterThan(0);
	await expect(page.getByText(/demo|sample node/i)).toHaveCount(0);

	const axisSelect = page.getByLabel('Wheel axis');
	await expect(axisSelect.locator('option[value="play_count"]')).toHaveJSProperty(
		'disabled',
		false
	);
	await expect(axisSelect.locator('option[value="decade"]')).toHaveJSProperty('disabled', true);
	await expect(axisSelect.locator('option[value="overplayed_ness"]')).toHaveJSProperty(
		'disabled',
		true
	);
	await expect(axisSelect.locator('option[value="set_played_in"]')).toHaveJSProperty(
		'disabled',
		true
	);

	const reasonByKey = Object.fromEntries(payload.axes.map((axis) => [axis.key, axis.reason]));
	expect(reasonByKey.decade ?? '').toContain('release year');
	expect(reasonByKey.overplayed_ness ?? '').toContain('popularity-curve');
	expect(reasonByKey.set_played_in ?? '').toContain('SET-08');
	expect(await axisSelect.locator('option[value="decade"]').getAttribute('title')).toBe(
		reasonByKey.decade
	);
	expect(await axisSelect.locator('option[value="overplayed_ness"]').getAttribute('title')).toBe(
		reasonByKey.overplayed_ness
	);
	expect(await axisSelect.locator('option[value="set_played_in"]').getAttribute('title')).toBe(
		reasonByKey.set_played_in
	);

	const playlistResponse = page.waitForResponse(
		(response) =>
			response.url().includes('/api/v1/library/wheel') &&
			response.url().includes('axis=playlist') &&
			response.status() === 200
	);
	await axisSelect.selectOption('playlist');
	const playlistPayload = await (await playlistResponse).json();
	expect(isWheelPayload(playlistPayload)).toBe(true);
	if (!isWheelPayload(playlistPayload)) return;
	await expect(summary).toContainText(
		new RegExp(
			`${playlistPayload.total_tracks}\\s*tracks\\s*${playlistPayload.families.length}\\s*families`
		)
	);

	mkdirSync(SCREENSHOT_DIR, { recursive: true });
	await page.screenshot({ path: join(SCREENSHOT_DIR, 'library-wheel-1280x800.png') });

	expect(failedResources.filter(({ url }) => !url.endsWith('/favicon.svg'))).toEqual([]);
	// A repo checkout names its own build (app_version from tauri.conf.json,
	// aea4c86d63) and answers /update/check with HTTP 200 whatever the channel
	// says; a 502 from this route is a defect (update_channel.py). The channel
	// verdict itself depends on the network, so assert the identity instead.
	const updateCheckResponse = await updateCheck;
	expect(updateCheckResponse.status()).toBe(200);
	const updateCheckBody = await updateCheckResponse.json();
	expect(updateCheckBody.current_version).toMatch(/^\d+\.\d+\.\d+/);
	expect(updateCheckBody.current_git_sha).toMatch(/^[0-9a-f]{8}$/);
	expect(consoleErrors.filter((error) => !error.startsWith('Failed to load resource:'))).toEqual(
		[]
	);
});
