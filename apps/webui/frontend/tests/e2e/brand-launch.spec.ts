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

/**
 * Every CSS animation the launch overlay owns, sorted. The overlay is pure CSS,
 * so `page.clock` cannot move it: these are driven through the Web Animations
 * API instead, which is the only seam that makes the phase assertions below
 * independent of how loaded the machine is.
 */
const LAUNCH_ANIMATION_NAMES = ['brand-half-slide-dark', 'brand-half-slide-light', 'launch-fade'];

/** Cold-open with a fresh launch marker; pause CSS until visible so slow loads cannot miss the 1.6 s window. */
async function openWithFreshLaunch(
	page: import('@playwright/test').Page,
	url: string,
	opts?: { clock?: boolean; keepPaused?: boolean }
): Promise<import('@playwright/test').Locator> {
	// The id travels as an ARGUMENT: an init script is serialized and evaluated
	// in the page, where this module's constants do not exist, so a closure
	// reference to one throws before the style is ever appended.
	await page.addInitScript((pauseStyleId: string) => {
		localStorage.removeItem('odj.brand-launch.v1');
		const pause = document.createElement('style');
		pause.id = pauseStyleId;
		pause.textContent =
			'.brand-launch, .brand-half-dark, .brand-half-light { animation-play-state: paused !important; }';
		(document.head ?? document.documentElement).appendChild(pause);
	}, BRAND_LAUNCH_PAUSE_STYLE_ID);
	if (opts?.clock) {
		await page.clock.install({ time: 0 });
	}
	const launch = page.getByLabel('Open DJ launch animation');
	await Promise.all([
		page.goto(url, { waitUntil: 'domcontentloaded' }),
		launch.waitFor({ state: 'visible', timeout: 30_000 })
	]);
	// The hold must be PRESENT, not merely un-errored. A pause that silently
	// never landed reads exactly like a pause that worked, right up until the
	// overlay it was meant to freeze has already played out.
	await expect(page.locator(`#${BRAND_LAUNCH_PAUSE_STYLE_ID}`)).toHaveCount(1);
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
		const project = (point: number[]) => (point[0] - cx) * nx + (point[1] - cy) * ny;
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

/** Take every launch animation under test control; returns the names captured. */
async function seizeLaunchAnimations(page: import('@playwright/test').Page): Promise<string[]> {
	return page.evaluate((names) => {
		const captured: string[] = [];
		for (const animation of document.getAnimations()) {
			const name = (animation as Animation & { animationName?: string }).animationName;
			if (name === undefined || !names.includes(name)) continue;
			animation.pause();
			captured.push(name);
		}
		return captured.sort();
	}, LAUNCH_ANIMATION_NAMES);
}

/** Seek every launch animation to the same offset from first paint (ms). */
async function seekLaunchTo(
	page: import('@playwright/test').Page,
	elapsedMs: number
): Promise<string[]> {
	return page.evaluate(
		({ names, offsetMs }) => {
			for (const animation of document.getAnimations()) {
				const name = (animation as Animation & { animationName?: string }).animationName;
				if (name === undefined || !names.includes(name)) continue;
				animation.pause();
				animation.currentTime = offsetMs;
			}
			// Reading computed style pulls the seeked frame in before the next call.
			return ['.brand-half-dark', '.brand-half-light'].map((selector) => {
				const element = document.querySelector(selector);
				return element === null ? 'missing' : getComputedStyle(element).transform;
			});
		},
		{ names: LAUNCH_ANIMATION_NAMES, offsetMs: elapsedMs }
	);
}

/** Hand the overlay back to the wall clock so its real fade can finish. */
async function releaseLaunchAnimations(page: import('@playwright/test').Page): Promise<void> {
	await page.evaluate(
		({ names, styleId }) => {
			document.getElementById(styleId)?.remove();
			for (const animation of document.getAnimations()) {
				const name = (animation as Animation & { animationName?: string }).animationName;
				if (name === undefined || !names.includes(name)) continue;
				// play() on a finished animation rewinds it to zero, which would
				// replay the slide the assertions above just walked through.
				if (animation.playState !== 'finished') animation.play();
			}
		},
		{ names: LAUNCH_ANIMATION_NAMES, styleId: BRAND_LAUNCH_PAUSE_STYLE_ID }
	);
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
	// Held at its first frame by the init-script pause, then walked phase by
	// phase with the Web Animations API. `page.clock` is installed for the app's
	// own timers but drives none of this: CSS animations run off the document
	// timeline, so a clock-advanced assertion was really reading whatever frame
	// wall time had reached, and on a loaded runner that frame was the wrong one.
	const launch = await openWithFreshLaunch(page, '/', { clock: true, keepPaused: true });
	await expect(launch).toBeVisible();
	expect(await seizeLaunchAnimations(page)).toEqual(LAUNCH_ANIMATION_NAMES);

	await seekLaunchTo(page, 0);
	await expect.poll(() => gapAlongDivider(page)).toBeGreaterThan(0);
	const startMagnitude = await halfTranslateMagnitude(page);
	expect(startMagnitude).toBeGreaterThan(1);

	// Mid-slide rather than one millisecond short of the meet: the halves ease
	// out, so that last millisecond is sub-pixel and proves nothing either way.
	// Closer than the start AND still apart is the claim that means something.
	await seekLaunchTo(page, Math.round(BRAND_LAUNCH_MEET_MS / 2));
	await expect.poll(() => halfTranslateMagnitude(page)).toBeGreaterThan(0.5);
	expect(await halfTranslateMagnitude(page)).toBeLessThan(startMagnitude);

	await seekLaunchTo(page, BRAND_LAUNCH_MEET_MS);
	await expect.poll(() => halfTranslateMagnitude(page)).toBeLessThan(0.5);
	expect(await gapAlongDivider(page)).toBeLessThan(1.5);

	await seekLaunchTo(page, BRAND_LAUNCH_FADE_START_MS);
	await expect(launch).toBeVisible();
	expect(await gapAlongDivider(page)).toBeLessThan(1.5);

	await releaseLaunchAnimations(page);
	await expect(launch).toHaveCount(0, { timeout: BRAND_LAUNCH_FADE_MS + 5_000 });
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
