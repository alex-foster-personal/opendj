// requirement: UX-EXPLAIN-02
// [if] hover performance explainers [then] popover teaches split/LINK/MIX/mode, else stop
import { expect, test, type Locator, type Page } from '@playwright/test';
import {
	closeAudioIoPanel,
	dismissExplainerChrome,
	explainerPop,
	explainerWrap,
	ensureMoreDeckLayout,
	ensureWideViewportForTopBarChrome,
	openAudioIoPanel,
	visibleHeadphoneMix,
	visibleHeadphoneOutputMode
} from './support/performance-visible-layout';
import { waitForPerformanceIpc } from './support/performance-ready';

const DEMO_KEYFRAME_IGNORE = new Set([
	'offset',
	'computedOffset',
	'easing',
	'composite',
	'transformOrigin',
	'transform-origin'
]);

async function expectExplainerPop(page: Page, control: Locator, pattern: RegExp): Promise<void> {
	await dismissExplainerChrome(page);
	await expect(control, 'explainer target must be visible in the supported layout').toBeVisible();
	await explainerWrap(control).hover();
	await expect(explainerPop(page).first()).toBeVisible({
		timeout: 2500
	});
	await expect(explainerPop(page).locator('.head').first()).toContainText(pattern);
}

async function expectDemoAnimationsPerformant(page: Page): Promise<void> {
	const result = await page.evaluate(
		(ignoreKeys) => {
			const ignore = new Set(ignoreKeys);
			const demoRoot = document.querySelector('[data-explainer-pop] .demo');
			if (demoRoot === null) {
				return { ok: false, reason: 'no [data-explainer-pop] .demo root' };
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
				if (effect === null || typeof (effect as KeyframeEffect).getKeyframes !== 'function')
					continue;
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
		},
		[...DEMO_KEYFRAME_IGNORE]
	);

	expect(result.ok, result.reason).toBe(true);
}

test('issue 3982: performance explainers and vibe thumb colors', async ({ page }) => {
	await page.addInitScript(() => sessionStorage.setItem('odj.enrich-card.hidden', '1'));
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await waitForPerformanceIpc(page);
	await ensureWideViewportForTopBarChrome(page);
	await ensureMoreDeckLayout(page);

	const fxPanel = page.getByRole('button', { name: 'FX panel' });
	const twoDeck = page.getByRole('button', { name: '2 deck view' });
	const link = page.getByRole('button', { name: 'LINK', exact: true });
	const mixKnob = visibleHeadphoneMix(page);
	const outputMode = visibleHeadphoneOutputMode(page);

	await expectExplainerPop(page, fxPanel, /fx/i);
	await expectExplainerPop(page, twoDeck, /2.?deck/i);
	await expectExplainerPop(page, link, /LINK/i);
	await expectExplainerPop(page, fxPanel, /fx/i);
	await expectDemoAnimationsPerformant(page);
	await expectExplainerPop(page, mixKnob, /MIX/i);

	await openAudioIoPanel(page);
	await expectExplainerPop(page, outputMode, /practice|split|output/i);
	await closeAudioIoPanel(page);

	const upThumb = page.locator('.vibe-thumb.up');
	const downThumb = page.locator('.vibe-thumb.down');
	await expect(upThumb).toBeVisible();
	await expect(downThumb).toBeVisible();
	const upColor = await upThumb.evaluate((el) => getComputedStyle(el).color);
	const downColor = await downThumb.evaluate((el) => getComputedStyle(el).color);
	const parseRgb = (css: string): number[] => {
		const legacy = css.match(/rgba?\(([^)]+)\)/);
		if (legacy !== null) {
			return legacy[1].split(',').map((v) => Number.parseFloat(v.trim()));
		}
		const srgb = css.match(/color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/);
		if (srgb !== null) {
			return [Number(srgb[1]), Number(srgb[2]), Number(srgb[3])].map((v) => Math.round(v * 255));
		}
		return [];
	};
	const [, ug, ub] = parseRgb(upColor);
	const [dr] = parseRgb(downColor);
	expect(ug).toBeGreaterThan(100);
	expect(ub).toBeLessThan(120);
	expect(dr).toBeGreaterThan(80);
});
