// requirement: OPS-10
// requirement: OPS-33
// requirement: PERF-UI-03
import { expect, test } from '@playwright/test';

import {
	BRAND_LAUNCH_DURATION_MS,
	BRAND_LAUNCH_FADE_MS,
	BRAND_LAUNCH_FADE_START_MS,
	BRAND_LAUNCH_HOLD_MS,
	BRAND_LAUNCH_MEET_MS
} from '../../src/lib/brand-launch';

/** Slide offset magnitude for one half (px in screen space). */
async function halfTranslateMagnitude(page: import('@playwright/test').Page): Promise<number> {
	return page.evaluate(() => {
		const dark = document.querySelector('[data-testid="brand-launch-half-dark"]');
		if (!dark) return NaN;
		const transform = getComputedStyle(dark).transform;
		if (!transform || transform === 'none') return 0;
		const matrix = new DOMMatrixReadOnly(transform);
		return Math.hypot(matrix.m41, matrix.m42);
	});
}

/** Separation along the NW-SE divider normal between the two half bounding boxes. */
async function gapAlongDivider(page: import('@playwright/test').Page): Promise<number> {
	return page.evaluate(() => {
		const darkEl = document.querySelector('[data-testid="brand-launch-half-dark"]');
		const lightEl = document.querySelector('[data-testid="brand-launch-half-light"]');
		if (!darkEl || !lightEl) return NaN;
		const d = darkEl.getBoundingClientRect();
		const l = lightEl.getBoundingClientRect();
		const nx = -Math.SQRT1_2;
		const ny = Math.SQRT1_2;
		const corners = (r: DOMRect) => [
			[r.left, r.top],
			[r.right, r.top],
			[r.right, r.bottom],
			[r.left, r.bottom]
		];
		const all = [...corners(d), ...corners(l)];
		const cx = all.reduce((sum, [x]) => sum + x, 0) / all.length;
		const cy = all.reduce((sum, [, y]) => sum + y, 0) / all.length;
		const project = ([x, y]: [number, number]) => (x - cx) * nx + (y - cy) * ny;
		const darkProj = corners(d).map(project);
		const lightProj = corners(l).map(project);
		const bboxGap = Math.max(0, Math.min(...lightProj) - Math.max(...darkProj));

		const transformAlongDivider = (el: Element) => {
			const transform = getComputedStyle(el).transform;
			if (!transform || transform === 'none') return 0;
			const matrix = new DOMMatrixReadOnly(transform);
			return matrix.m41 * nx + matrix.m42 * ny;
		};
		const darkTransform = getComputedStyle(darkEl).transform;
		const darkMag =
			darkTransform && darkTransform !== 'none'
				? Math.hypot(
						new DOMMatrixReadOnly(darkTransform).m41,
						new DOMMatrixReadOnly(darkTransform).m42
					)
				: 0;
		const transformGap = Math.max(
			0,
			transformAlongDivider(darkEl) - transformAlongDivider(lightEl)
		);
		// Semicircle bbox overlap hides the cut-line gap while halves are still apart.
		if (bboxGap <= 0 && darkMag > 25) {
			return transformGap;
		}
		return bboxGap;
	});
}

test('first open plays the identity launch once without blocking the app', async ({ page }) => {
	await page.addInitScript(() => localStorage.removeItem('odj.brand-launch.v1'));
	await page.goto('/');
	const launch = page.getByLabel('Open DJ launch animation');
	await expect(launch).toBeVisible({ timeout: 15_000 });
	await expect(page.locator('body')).toBeVisible();
	await expect(launch).toBeHidden({ timeout: 5_000 });
	await expect.poll(() => page.evaluate(() => localStorage.getItem('odj.brand-launch.v1'))).toBe('complete');

	await page.reload();
	await expect(launch).toBeHidden();
});

test('launch halves slide closed then fade removes overlay', async ({ page }) => {
	await page.clock.install({ time: 0 });
	await page.addInitScript(() => localStorage.removeItem('odj.brand-launch.v1'));
	const launch = page.getByLabel('Open DJ launch animation');
	const launchVisible = launch.waitFor({ state: 'visible', timeout: 15_000 });
	await page.goto('/');
	await launchVisible;

	await expect(launch).toBeVisible();

	const gapStart = await gapAlongDivider(page);
	expect(gapStart).toBeGreaterThan(0);
	expect(await halfTranslateMagnitude(page)).toBeGreaterThan(1);

	let elapsedMs = 0;
	const advanceTo = async (targetMs: number) => {
		const delta = targetMs - elapsedMs;
		if (delta > 0) {
			await page.clock.runFor(delta);
			elapsedMs = targetMs;
		}
	};

	await advanceTo(BRAND_LAUNCH_MEET_MS - 1);
	expect(await halfTranslateMagnitude(page)).toBeGreaterThan(0.5);

	await advanceTo(BRAND_LAUNCH_MEET_MS);
	while ((await halfTranslateMagnitude(page)) > 0.5) {
		await page.clock.runFor(16);
		elapsedMs += 16;
	}
	expect(await gapAlongDivider(page)).toBeLessThan(1.5);

	await advanceTo(BRAND_LAUNCH_FADE_START_MS);
	await expect(launch).toBeVisible();
	expect(await gapAlongDivider(page)).toBeLessThan(1.5);

	await page.clock.runFor(BRAND_LAUNCH_FADE_MS + 50);
	await expect(launch).toHaveCount(0);
	await expect.poll(() => page.evaluate(() => localStorage.getItem('odj.brand-launch.v1'))).toBe(
		'complete'
	);
});

test('reduced motion shows closed circle without slide', async ({ page }) => {
	await page.emulateMedia({ reducedMotion: 'reduce' });
	await page.clock.install({ time: 0 });
	await page.addInitScript(() => localStorage.removeItem('odj.brand-launch.v1'));
	const launch = page.getByLabel('Open DJ launch animation');
	const launchVisible = launch.waitFor({ state: 'visible', timeout: 15_000 });
	await page.goto('/');
	await launchVisible;

	await expect(launch).toBeVisible();
	expect(await gapAlongDivider(page)).toBeLessThan(1.5);

	await page.clock.runFor(BRAND_LAUNCH_DURATION_MS + 50);
	await expect(launch).toHaveCount(0);
	await expect.poll(() => page.evaluate(() => localStorage.getItem('odj.brand-launch.v1'))).toBe(
		'complete'
	);
});

test('library rows paint behind the launch overlay on /performance', async ({ page }) => {
	test.skip(process.env.PERFORMANCE_E2E_FIXTURE === '1', 'needs reference library rows');
	await page.addInitScript(() => localStorage.removeItem('odj.brand-launch.v1'));
	const launch = page.getByLabel('Open DJ launch animation');
	const rows = page.locator('[data-testid="track-row"]').first();
	await Promise.all([
		page.goto('/performance'),
		launch.waitFor({ state: 'visible', timeout: 30_000 })
	]);
	// Freeze the launch overlay while library rows finish painting (PERF-UI-03).
	await page.clock.install({ time: 0 });
	await expect(rows).toBeVisible({ timeout: 15_000 });
	await expect(launch).toBeVisible();
});
