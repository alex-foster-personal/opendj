import { expect, test } from '@playwright/test';

/**
 * Exercises `drawPlayhead` (src/lib/components/rb/wave/render.ts) through a
 * REAL `<canvas>` 2D context in a real browser, replacing
 * tests/unit/playhead-weight.test.mjs's hand-written recorder object that
 * only proved calls were made against a fake renderer (r3915742281).
 *
 * `getImageData` reads back what the browser actually composited: this
 * catches anything a call-recording spy structurally cannot, notably a
 * `globalAlpha` that a real canvas clamps, coerces, or composites
 * differently than a JS setter spy would ever notice.
 *
 * No backend needed: `render.ts` has no fetching side effects at module
 * load, so this runs under the default (vite-only) playwright.config.ts.
 */

const W = 100;
const H = 40;
const CENTER_X = Math.round(W / 2); // 50, matching drawPlayhead's own rounding

/** Reads back one pixel {r,g,b,a} from the page's #stage canvas. */
async function pixelAt(page: import('@playwright/test').Page, x: number, y: number) {
	return page.evaluate(
		([x, y]) => {
			const canvas = document.getElementById('stage') as HTMLCanvasElement;
			const ctx = canvas.getContext('2d')!;
			const [r, g, b, a] = ctx.getImageData(x, y, 1, 1).data;
			return { r, g, b, a };
		},
		[x, y] as const
	);
}

/** Imports the real module and paints one tone onto the real canvas, having
 * cleared it first so a prior paint cannot leak into the read-back. */
async function paint(page: import('@playwright/test').Page, tone: string, timeMs = 0) {
	await page.evaluate(
		async ({ tone, timeMs, w, h }) => {
			const mod = await import(new URL('/src/lib/components/rb/wave/render.ts', location.href).href);
			const canvas = document.getElementById('stage') as HTMLCanvasElement;
			const ctx = canvas.getContext('2d')!;
			ctx.clearRect(0, 0, w, h);
			mod.drawPlayhead(ctx, w, h, tone, timeMs);
		},
		{ tone, timeMs, w: W, h: H }
	);
}

test.beforeEach(async ({ page }) => {
	await page.goto('/');
	await page.evaluate(
		({ w, h }) => {
			const canvas = document.createElement('canvas');
			canvas.id = 'stage';
			canvas.width = w;
			canvas.height = h;
			document.body.appendChild(canvas);
		},
		{ w: W, h: H }
	);
});

const STATIC_TONES = ['now', 'master', 'bar1', 'synced'];
const TONE_HEX: Record<string, [number, number, number]> = {
	now: [0xe2, 0x3a, 0x32],
	master: [0xe0, 0xcc, 0x6e],
	bar1: [0x35, 0xc0, 0x4f],
	synced: [0x7e, 0xd9, 0x92]
};
/** Per-tone glow alpha, mirroring `drawPlayhead`'s static branches: the glow
 * carries the tone's weight (bar1 heaviest, synced lightest), the core is
 * always opaque. Read back as round(alpha * 255) with a small compositing
 * tolerance. */
const TONE_GLOW_ALPHA: Record<string, number> = {
	now: 0.4,
	master: 0.5,
	bar1: 0.55,
	synced: 0.32
};
const GLOW_TOLERANCE = 3;

test.describe('playhead weight, painted on a real canvas', () => {
	for (const tone of STATIC_TONES) {
		test(`${tone}: glow spans exactly 3px at its own alpha, core is 1px opaque`, async ({
			page
		}) => {
			await paint(page, tone);
			// Outside the glow: fully transparent.
			expect((await pixelAt(page, CENTER_X - 2, 20)).a).toBe(0);
			expect((await pixelAt(page, CENTER_X + 2, 20)).a).toBe(0);
			// Glow-only columns (core does not cover them): the tone's own alpha.
			const expectedGlow = Math.round(TONE_GLOW_ALPHA[tone] * 255);
			const left = await pixelAt(page, CENTER_X - 1, 20);
			const right = await pixelAt(page, CENTER_X + 1, 20);
			for (const p of [left, right]) expect(p.a).toBeGreaterThanOrEqual(expectedGlow - GLOW_TOLERANCE);
			for (const p of [left, right]) expect(p.a).toBeLessThanOrEqual(expectedGlow + GLOW_TOLERANCE);
			// Core column: opaque, drawn on top of the glow at alpha 1.
			const core = await pixelAt(page, CENTER_X, 20);
			expect(core.a).toBe(255);
			const [r, g, b] = TONE_HEX[tone];
			expect([core.r, core.g, core.b]).toEqual([r, g, b]);
		});
	}

	test('every static tone paints the identical geometry - only colour and glow weight differ', async ({
		page
	}) => {
		const colors: string[] = [];
		const glows: number[] = [];
		for (const tone of STATIC_TONES) {
			await paint(page, tone);
			const left = await pixelAt(page, CENTER_X - 1, 20);
			const right = await pixelAt(page, CENTER_X + 1, 20);
			const core = await pixelAt(page, CENTER_X, 20);
			// Same footprint for every tone: symmetric 1px glow flanks, opaque core.
			expect((await pixelAt(page, CENTER_X - 2, 20)).a).toBe(0);
			expect((await pixelAt(page, CENTER_X + 2, 20)).a).toBe(0);
			expect(left.a).toBe(right.a);
			expect(left.a).toBeGreaterThan(0);
			expect(core.a).toBe(255);
			glows.push(left.a);
			colors.push(`${core.r},${core.g},${core.b}`);
		}
		// Colour tells the tones apart, and so does the glow weight (bar1 > master > now > synced).
		expect(new Set(colors).size).toBe(STATIC_TONES.length);
		const byTone = Object.fromEntries(STATIC_TONES.map((tone, i) => [tone, glows[i]]));
		expect(byTone.bar1).toBeGreaterThan(byTone.master);
		expect(byTone.master).toBeGreaterThan(byTone.now);
		expect(byTone.now).toBeGreaterThan(byTone.synced);
	});

	test('a synced master paints pure yellow then pure green adjacent cores', async ({ page }) => {
		await paint(page, 'masterSynced');
		const yellowCore = await pixelAt(page, CENTER_X - 1, 20);
		const greenCore = await pixelAt(page, CENTER_X, 20);
		// This catches the wrong paint order: a green glow painted after the
		// yellow core composites over it and can never read back as pure yellow.
		expect(yellowCore).toEqual({ r: 224, g: 204, b: 110, a: 255 });
		expect(greenCore).toEqual({ r: 126, g: 217, b: 146, a: 255 });
		for (const x of [CENTER_X - 2, CENTER_X + 1]) {
			const glow = await pixelAt(page, x, 20);
			expect(glow.a).toBeGreaterThan(105);
			expect(glow.a).toBeLessThan(125);
		}
	});

	test('an unsynced master stays yellow-only and followers gain no master core', async ({ page }) => {
		await paint(page, 'master');
		expect(await pixelAt(page, CENTER_X, 20)).toEqual({ r: 224, g: 204, b: 110, a: 255 });
		for (const followerTone of ['bar1', 'synced', 'drift']) {
			await paint(page, followerTone, 250);
			const leftOfCore = await pixelAt(page, CENTER_X - 1, 20);
			expect(leftOfCore.a).toBeLessThan(255);
		}
	});

	test('drift keeps pulsing: glow alpha differs across time', async ({ page }) => {
		await paint(page, 'drift', 0);
		const a0 = (await pixelAt(page, CENTER_X - 1, 20)).a;
		await paint(page, 'drift', 250);
		const a250 = (await pixelAt(page, CENTER_X - 1, 20)).a;
		expect(a0).not.toBe(a250);
	});
});
