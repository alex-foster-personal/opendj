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
 * Every element the launch overlay animates, sorted. The overlay is pure CSS,
 * so `page.clock` cannot move it: these are driven through the Web Animations
 * API instead, which is the only seam that makes the phase assertions below
 * independent of how loaded the machine is.
 *
 * Selected by TARGET, not by animation name: svelte rewrites the `@keyframes`
 * names in a component's scoped style block, so the names in BrandLaunch.svelte
 * are not the names `Animation.animationName` reports at runtime.
 */
const LAUNCH_ANIMATED_SELECTORS = ['.brand-half-dark', '.brand-half-light', '.brand-launch'];

/** Cold-open with a fresh launch marker; pause CSS until visible so slow loads cannot miss the 1.6 s window. */
async function openWithFreshLaunch(
	page: import('@playwright/test').Page,
	url: string,
	opts?: { clock?: boolean; keepPaused?: boolean }
): Promise<import('@playwright/test').Locator> {
	// Two reasons this hold never landed before, both silent. The id travels as
	// an ARGUMENT because an init script is serialized and evaluated in the page,
	// where this module's constants do not exist. And the append waits for a root
	// because an init script runs before <html> is parsed, so head and
	// documentElement are BOTH null at the moment it first runs.
	await page.addInitScript((pauseStyleId: string) => {
		localStorage.removeItem('odj.brand-launch.v1');
		const install = (): boolean => {
			const root = document.head ?? document.documentElement;
			if (root === null) return false;
			if (document.getElementById(pauseStyleId) !== null) return true;
			const pause = document.createElement('style');
			pause.id = pauseStyleId;
			pause.textContent =
				'.brand-launch, .brand-half-dark, .brand-half-light { animation-play-state: paused !important; }';
			root.appendChild(pause);
			return true;
		};
		if (install()) return;
		// A MutationObserver is the earliest hook that survives `page.clock`:
		// its callback is a microtask, where setTimeout and rAF are both frozen.
		const observer = new MutationObserver(() => {
			if (install()) observer.disconnect();
		});
		observer.observe(document, { childList: true, subtree: true });
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

/** Take every launch animation under test control; returns the targets captured. */
async function seizeLaunchAnimations(page: import('@playwright/test').Page): Promise<string[]> {
	return page.evaluate((selectors) => {
		const captured: string[] = [];
		for (const animation of document.getAnimations()) {
			const target = (animation.effect as KeyframeEffect | null)?.target ?? null;
			const selector = selectors.find((candidate) => target?.matches(candidate) === true);
			if (selector === undefined) continue;
			animation.pause();
			captured.push(selector);
		}
		return captured.sort();
	}, LAUNCH_ANIMATED_SELECTORS);
}

/** Seek every launch animation to the same offset from first paint (ms). */
async function seekLaunchTo(
	page: import('@playwright/test').Page,
	elapsedMs: number
): Promise<string[]> {
	return page.evaluate(
		({ selectors, offsetMs }) => {
			for (const animation of document.getAnimations()) {
				const target = (animation.effect as KeyframeEffect | null)?.target ?? null;
				if (!selectors.some((candidate) => target?.matches(candidate) === true)) continue;
				animation.pause();
				animation.currentTime = offsetMs;
			}
			// Reading computed style pulls the seeked frame in before the next call.
			return ['.brand-half-dark', '.brand-half-light'].map((selector) => {
				const element = document.querySelector(selector);
				return element === null ? 'missing' : getComputedStyle(element).transform;
			});
		},
		{ selectors: LAUNCH_ANIMATED_SELECTORS, offsetMs: elapsedMs }
	);
}

/** Hand the overlay back to the wall clock so its real fade can finish. */
async function releaseLaunchAnimations(page: import('@playwright/test').Page): Promise<void> {
	await page.evaluate(
		({ selectors, styleId }) => {
			document.getElementById(styleId)?.remove();
			for (const animation of document.getAnimations()) {
				const target = (animation.effect as KeyframeEffect | null)?.target ?? null;
				if (!selectors.some((candidate) => target?.matches(candidate) === true)) continue;
				// play() on a finished animation rewinds it to zero, which would
				// replay the slide the assertions above just walked through.
				if (animation.playState !== 'finished') animation.play();
			}
		},
		{ selectors: LAUNCH_ANIMATED_SELECTORS, styleId: BRAND_LAUNCH_PAUSE_STYLE_ID }
	);
}

test('first open plays the identity launch once without blocking the app', async ({ page }) => {
	const launch = await openWithFreshLaunch(page, '/');
	await expect(page.locator('body')).toBeVisible();
	await expect(launch).toBeHidden({ timeout: BRAND_LAUNCH_DURATION_MS + 5_000 });
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
	expect(await seizeLaunchAnimations(page)).toEqual(LAUNCH_ANIMATED_SELECTORS);

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
