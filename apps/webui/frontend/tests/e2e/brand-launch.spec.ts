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

const BRAND_LAUNCH_PAUSE_STYLE_ID = 'brand-launch-test-pause';

/** Cold-open with a fresh launch marker; pause CSS until visible so slow loads cannot miss the 1.6 s window. */
async function openWithFreshLaunch(
	page: import('@playwright/test').Page,
	url: string,
	opts?: { clock?: boolean; keepPaused?: boolean }
): Promise<import('@playwright/test').Locator> {
	await page.addInitScript(() => {
		localStorage.removeItem('odj.brand-launch.v1');
		const pause = document.createElement('style');
		pause.id = BRAND_LAUNCH_PAUSE_STYLE_ID;
		pause.textContent =
			'.brand-launch, .brand-half-dark, .brand-half-light { animation-play-state: paused !important; }';
		(document.head ?? document.documentElement).appendChild(pause);
	});
	if (opts?.clock) {
		await page.clock.install({ time: 0 });
	}
	const launch = page.getByLabel('Open DJ launch animation');
	await Promise.all([
		page.goto(url, { waitUntil: 'domcontentloaded' }),
		launch.waitFor({ state: 'visible', timeout: 30_000 })
	]);
	if (!opts?.keepPaused) {
		await page.evaluate((styleId) => document.getElementById(styleId)?.remove(), BRAND_LAUNCH_PAUSE_STYLE_ID);
	}
	return launch;
}

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
	const launch = await openWithFreshLaunch(page, '/');
	await expect(page.locator('body')).toBeVisible();
	await expect(launch).toBeHidden({ timeout: BRAND_LAUNCH_DURATION_MS + 1_000 });
	await expect.poll(() => page.evaluate(() => localStorage.getItem('odj.brand-launch.v1'))).toBe('complete');

	await page.reload();
	await expect(launch).toBeHidden();
});

test('launch halves slide closed then fade removes overlay', async ({ page }) => {
	const launch = await openWithFreshLaunch(page, '/', { clock: true });
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
	const launch = await openWithFreshLaunch(page, '/', { clock: true });
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
	const launch = await openWithFreshLaunch(page, '/performance', { clock: true, keepPaused: true });
	const rows = page.locator('[data-testid="track-row"]').first();
	await expect(rows).toBeVisible({ timeout: 15_000 });
	await expect(launch).toBeVisible();
});
