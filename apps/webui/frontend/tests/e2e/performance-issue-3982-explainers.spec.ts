// requirement: UX-EXPLAIN-02
// [if] hover performance explainers [then] popover teaches split/LINK/MIX/mode, else stop
import { expect, test } from '@playwright/test';
import { waitForPerformanceIpc } from './support/performance-ready';

const DEMO_KEYFRAME_IGNORE = new Set([
	'offset',
	'computedOffset',
	'easing',
	'composite',
	'transformOrigin',
	'transform-origin'
]);

async function expectExplainerPop(page: import('@playwright/test').Page, trigger: string, pattern: RegExp): Promise<void> {
	const control = page.locator(trigger).first();
	await control.hover();
	await expect(page.locator('.explainer .pop').filter({ has: page.locator('.head') }).first()).toBeVisible({
		timeout: 2500
	});
	await expect(page.locator('.explainer .pop .head').first()).toContainText(pattern);
}

async function expectDemoAnimationsPerformant(page: import('@playwright/test').Page): Promise<void> {
	const result = await page.evaluate((ignoreKeys) => {
		const ignore = new Set(ignoreKeys);
		const demoRoot = document.querySelector('.explainer .pop .demo');
		if (demoRoot === null) {
			return { ok: false, reason: 'no .explainer .pop .demo root' };
		}
		const animated = demoRoot.querySelector('.fx-bar-a, .link-beat, .mix-knob-cap, .cue-ph');
		if (animated === null) {
			return { ok: false, reason: 'no known animated demo element under popover' };
		}
		const animations = animated.getAnimations();
		if (animations.length === 0) {
			return { ok: false, reason: 'getAnimations() returned none' };
		}
		const running = animations.some((a) => a.playState === 'running');
		if (!running) {
			return { ok: false, reason: `playState=${animations.map((a) => a.playState).join(',')}` };
		}
		const bad: string[] = [];
		for (const animation of animations) {
			const effect = animation.effect;
			if (effect === null || typeof (effect as KeyframeEffect).getKeyframes !== 'function') continue;
			const frames = (effect as KeyframeEffect).getKeyframes();
			for (const frame of frames) {
				for (const key of Object.keys(frame)) {
					if (ignore.has(key)) continue;
					if (key !== 'transform' && key !== 'opacity') {
						bad.push(`${key}@${String(frame.offset ?? '?')}`);
					}
				}
			}
		}
		if (bad.length > 0) {
			return { ok: false, reason: `non-perf keyframe props: ${bad.join(', ')}` };
		}
		return { ok: true, reason: '' };
	}, [...DEMO_KEYFRAME_IGNORE]);

	expect(result.ok, result.reason).toBe(true);
}

test('issue 3982: performance explainers and vibe thumb colors', async ({ page }) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await waitForPerformanceIpc(page);

	await expectExplainerPop(page, 'button[aria-label="split view"]', /split/i);
	await page.mouse.move(0, 0);
	await expectExplainerPop(page, 'button[aria-label="FX panel"]', /fx/i);
	await page.mouse.move(0, 0);
	await expectExplainerPop(page, 'button[aria-label="2 deck view"]', /2.?deck/i);
	await page.mouse.move(0, 0);
	await expectExplainerPop(page, 'button.link-btn[aria-label="LINK"]', /LINK/i);
	await page.mouse.move(0, 0);
	await expectExplainerPop(page, 'button[aria-label="FX panel"]', /fx/i);
	await expectDemoAnimationsPerformant(page);
	await page.mouse.move(0, 0);
	await expectExplainerPop(page, '[aria-label="Headphone CUE to MASTER mix"]', /MIX/i);
	await page.mouse.move(0, 0);
	await expectExplainerPop(page, '.hp-mode', /practice|split|output/i);

	const upColor = await page.locator('button[aria-label="thumbs up"]').evaluate((el) => getComputedStyle(el).color);
	const downColor = await page.locator('button[aria-label="thumbs down"]').evaluate((el) => getComputedStyle(el).color);
	const parseRgb = (css: string): number[] => {
		const m = css.match(/rgba?\(([^)]+)\)/);
		if (m === null) return [];
		return m[1].split(',').map((v) => Number.parseFloat(v.trim()));
	};
	const [ug, , ub] = parseRgb(upColor);
	const [dr] = parseRgb(downColor);
	expect(ug).toBeGreaterThan(100);
	expect(ub).toBeLessThan(120);
	expect(dr).toBeGreaterThan(80);
});
