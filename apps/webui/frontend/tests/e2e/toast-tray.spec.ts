/**
 * The toast tray, driven through the real DOM rather than the store.
 *
 * The unit suite proves the store's timers, ids and payload. What only a real
 * browser can prove is that a POINTER over the rendered element holds it, that
 * the x is reachable and hits the right toast, and that the clipboard actually
 * receives the report - the clipboard in particular is a browser capability with
 * a secure-context precondition that no node:test stand-in exercises.
 *
 * Toasts are raised by importing the app's own store module from the Vite dev
 * server, which is the same module instance the layout renders, so nothing here
 * is a stand-in for the thing under test.
 */
import { expect, test, type Page } from '@playwright/test';

const STORE = '/src/lib/stores.svelte.ts';

async function raise(page: Page, message: string, kind = 'info', dismissMs = 60_000): Promise<string> {
	return page.evaluate(
		async ([store, msg, k, ms]) => {
			const mod = await import(/* @vite-ignore */ store as string);
			mod.pushToast(msg as string, k as 'info' | 'warn' | 'error', ms as number);
			return mod.toasts[mod.toasts.length - 1].logId as string;
		},
		[STORE, message, kind, dismissMs] as const
	);
}

test.beforeEach(async ({ page }) => {
	// The dev server here has no daemon behind it, so the app shell can still be
	// laying out when the first assertion runs. Waiting for the tray container
	// (which the layout always renders, empty or not) is the precondition that
	// actually means "the app is up", where a body visibility check is not.
	await page.goto('/');
	await page.locator('.toast-stack').waitFor({ state: 'attached' });
});

test('a toast renders with a dismiss control and a copy target', async ({ page }) => {
	const id = await raise(page, 'rendered toast');
	const toast = page.locator(`[data-toast-id="${id}"]`);
	await expect(toast).toBeVisible();
	await expect(toast).toContainText('rendered toast');
	await expect(page.locator(`[data-toast-dismiss="${id}"]`)).toBeVisible();
	await expect(page.locator(`[data-toast-copy="${id}"]`)).toBeVisible();
});

test('the x dismisses that toast and leaves the others', async ({ page }) => {
	const first = await raise(page, 'keep me');
	const second = await raise(page, 'remove me');

	await page.locator(`[data-toast-dismiss="${second}"]`).click();

	await expect(page.locator(`[data-toast-id="${second}"]`)).toHaveCount(0);
	await expect(page.locator(`[data-toast-id="${first}"]`)).toBeVisible();
});

// REQ: UX-TOAST-01
test('a pointer over a toast holds it past its dismissal delay', async ({ page }) => {
	const id = await raise(page, 'hover holds me', 'info', 1200);
	const toast = page.locator(`[data-toast-id="${id}"]`);
	await expect(toast).toBeVisible();

	await toast.hover();
	await page.waitForTimeout(2500); // well past the 1200ms it would have died at
	await expect(toast).toBeVisible();

	// And leaving restarts a full delay rather than firing the remainder.
	await page.mouse.move(0, 0);
	await page.waitForTimeout(600);
	await expect(toast).toBeVisible();
	await expect(toast).toHaveCount(0, { timeout: 4000 });
});

test('an untouched toast still fades, so the hold is doing the work', async ({ page }) => {
	const id = await raise(page, 'fade normally', 'info', 800);
	const toast = page.locator(`[data-toast-id="${id}"]`);
	await expect(toast).toBeVisible();
	await expect(toast).toHaveCount(0, { timeout: 5000 });
});

// REQ: UX-TOAST-01
test('clicking a toast copies a report whose id matches the logged id', async ({
	page,
	context
}) => {
	await context.grantPermissions(['clipboard-read', 'clipboard-write']);

	const consoleLines: string[] = [];
	page.on('console', (msg) => consoleLines.push(msg.text()));

	const id = await raise(page, 'copy me with my id', 'error');
	await page.locator(`[data-toast-copy="${id}"]`).click();
	await expect(page.locator(`[data-toast-copied="${id}"]`)).toBeVisible();

	const clipboard = await page.evaluate(() => navigator.clipboard.readText());

	// The payload carries what was asked for.
	expect(clipboard).toContain(`id: ${id}`);
	expect(clipboard).toContain('message: copy me with my id');
	expect(clipboard).toMatch(/^when: \d{4}-\d{2}-\d{2}T/m);
	expect(clipboard).toMatch(/^machine: /m);
	expect(clipboard).toMatch(/^user: /m);
	// A real browser names itself AND its version, which is what was asked for.
	expect(clipboard).toMatch(/^client: Chrome \d/m);

	// THE correlation: the id on the clipboard is the id in the log line.
	const logLine = consoleLines.find((l) => l.includes('[perf-event] toast-error'));
	expect(logLine, 'every toast writes a ring row').toBeTruthy();
	expect(logLine).toContain(`id=${id}`);
});

// pin 9bf12adccb45: "still show toast but make it orange warning instead of red
// and blocking". A BAR beat sync that folds to half/double tempo now HAPPENS
// and reports it; it is neither a failure nor a neutral note, and a two-value
// scale forced it to be one of those.
//
// Computed colour in a real browser, because "is it orange" is a question about
// what the compositor paints: a later rule, an !important or a media query
// could win it without any visible change to this component's own source.
//
// - if warn paints the same border as info the fold is invisible -> broken.
// - if warn paints the same border as error then a lock that SUCCEEDED still
//   reads as a failure, which is the thing the pin asked to stop -> broken.
test('a warn toast shows a visible click-to-copy hint', async ({ page }) => {
	const id = await raise(page, 'folded lock warning', 'warn');
	const hint = page.locator(`[data-toast-copy-hint="${id}"]`);
	await expect(hint).toBeVisible();
	await expect(hint).toHaveText('Click to copy');
});

test('wheeling over a toast dismisses it', async ({ page }) => {
	const id = await raise(page, 'wheel dismiss me', 'warn', 60_000);
	const toast = page.locator(`[data-toast-id="${id}"]`);
	await expect(toast).toBeVisible();
	await toast.dispatchEvent('wheel', { deltaY: 120 });
	await expect(toast).toHaveCount(0);
});

test('a warn toast paints its own colour, between info and error', async ({ page }) => {
	const info = await raise(page, 'plain note', 'info');
	const warn = await raise(page, 'folded lock', 'warn');
	const error = await raise(page, 'real failure', 'error');

	const border = async (id: string): Promise<string> =>
		page
			.locator(`[data-toast-id="${id}"]`)
			.evaluate((node) => getComputedStyle(node).borderTopColor);
	const infoBorder = await border(info);
	const warnBorder = await border(warn);
	const errorBorder = await border(error);

	expect(warnBorder, 'a warn toast must not look like a neutral note').not.toBe(infoBorder);
	expect(warnBorder, 'a successful-but-folded lock must not look like a failure').not.toBe(
		errorBorder
	);
	// And it is the declared warning token rather than an arbitrary third value.
	const declared = await page.evaluate(() => {
		const probe = document.createElement('span');
		document.body.appendChild(probe);
		probe.style.borderTopColor = 'var(--warning)';
		const resolved = getComputedStyle(probe).borderTopColor;
		probe.remove();
		return resolved;
	});
	expect(warnBorder).toBe(declared);
});

// REQ: UX-TOAST-03
// [if] five toasts pushed [then] DOM never shows more than three [else stop].
test('at most three toasts are visible when more are pushed', async ({ page }) => {
	await page.evaluate(
		async ([store]) => {
			const mod = await import(/* @vite-ignore */ store as string);
			for (let i = 0; i < 5; i += 1) {
				mod.pushToast(`stack toast ${i}`, 'error', 120_000);
			}
		},
		[STORE] as const
	);
	for (let frame = 0; frame < 8; frame += 1) {
		const count = await page.locator('[data-toast-id]').count();
		expect(count).toBeLessThanOrEqual(3);
		await page.waitForTimeout(16);
	}
	await expect(page.locator('[data-toast-id]')).toHaveCount(3, { timeout: 3000 });
});

// REQ: UX-TOAST-03
// [if] fourth toast evicts oldest [then] exiting class uses ~100ms transition [else stop].
test('the fourth toast evicts the oldest with an exiting marker', async ({ page }) => {
	type ExitEvidence = {
		exitingFound: boolean;
		transformMs: number;
		opacityMs: number;
		exitMs: number;
		exitPx: number;
		transformFound: boolean;
		opacityFound: boolean;
	};

	const first = await raise(page, 'oldest toast', 'error', 120_000);
	await raise(page, 'toast two', 'error', 120_000);
	await raise(page, 'toast three', 'error', 120_000);

	const evidence = await page.evaluate(
		async ([store, oldestId]): Promise<ExitEvidence> => {
			const parseMs = (raw: string): number => {
				const trimmed = raw.trim();
				const msMatch = /^(-?\d+(?:\.\d+)?)ms$/.exec(trimmed);
				if (msMatch) return Number(msMatch[1]);
				const sMatch = /^(-?\d+(?:\.\d+)?)s$/.exec(trimmed);
				if (sMatch) return Number(sMatch[1]) * 1000;
				return NaN;
			};
			const parsePx = (raw: string): number => {
				const trimmed = raw.trim();
				const pxMatch = /^(-?\d+(?:\.\d+)?)px$/.exec(trimmed);
				if (pxMatch) return Number(pxMatch[1]);
				return NaN;
			};
			const transitionDurationMs = (transition: CSSTransition | undefined): number => {
				if (transition?.effect === null || transition?.effect === undefined) return NaN;
				const timing = transition.effect.getTiming();
				const duration = timing.duration;
				return typeof duration === 'number' && Number.isFinite(duration) ? duration : NaN;
			};
			const raf = (): Promise<void> =>
				new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));

			const mod = await import(/* @vite-ignore */ store as string);
			mod.pushToast('toast four', 'error', 120_000);

			await raf();
			await raf();

			const findExiting = (): HTMLElement | null =>
				document.querySelector(`[data-toast-exiting="${oldestId}"]`);

			const collect = (node: HTMLElement): ExitEvidence => {
				const cssTransitions = node
					.getAnimations()
					.filter((animation): animation is CSSTransition => animation instanceof CSSTransition);
				const transformTransition = cssTransitions.find((t) => t.transitionProperty === 'transform');
				const opacityTransition = cssTransitions.find((t) => t.transitionProperty === 'opacity');
				const style = getComputedStyle(node);
				return {
					exitingFound: true,
					transformMs: transitionDurationMs(transformTransition),
					opacityMs: transitionDurationMs(opacityTransition),
					exitMs: parseMs(style.getPropertyValue('--toast-exit-ms')),
					exitPx: parsePx(style.getPropertyValue('--toast-exit-px')),
					transformFound: transformTransition !== undefined,
					opacityFound: opacityTransition !== undefined
				};
			};

			for (let frame = 0; frame < 8; frame += 1) {
				const node = findExiting();
				if (node === null) {
					await raf();
					continue;
				}
				const sample = collect(node);
				if (sample.transformFound === true && sample.opacityFound === true) {
					return sample;
				}
				await raf();
			}

			const node = findExiting();
			if (node === null) {
				return {
					exitingFound: false,
					transformMs: NaN,
					opacityMs: NaN,
					exitMs: NaN,
					exitPx: NaN,
					transformFound: false,
					opacityFound: false
				};
			}
			return collect(node);
		},
		[STORE, first] as const
	);

	expect(evidence.exitingFound, JSON.stringify(evidence)).toBe(true);
	expect(evidence.transformFound, JSON.stringify(evidence)).toBe(true);
	expect(evidence.opacityFound, JSON.stringify(evidence)).toBe(true);
	expect(evidence.transformMs, JSON.stringify(evidence)).toBeGreaterThanOrEqual(90);
	expect(evidence.transformMs, JSON.stringify(evidence)).toBeLessThanOrEqual(120);
	expect(evidence.opacityMs, JSON.stringify(evidence)).toBeGreaterThanOrEqual(90);
	expect(evidence.opacityMs, JSON.stringify(evidence)).toBeLessThanOrEqual(120);
	expect(evidence.exitMs, JSON.stringify(evidence)).toBeGreaterThanOrEqual(95);
	expect(evidence.exitMs, JSON.stringify(evidence)).toBeLessThanOrEqual(105);
	expect(evidence.exitPx, JSON.stringify(evidence)).toBeGreaterThanOrEqual(95);
	expect(evidence.exitPx, JSON.stringify(evidence)).toBeLessThanOrEqual(105);

	await expect(page.locator('[data-toast-id]')).toHaveCount(3);
	await expect(page.locator(`[data-toast-exiting="${first}"]`)).toHaveCount(0, { timeout: 5000 });
});

// REQ: UX-TOAST-03
// [if] long selection-load error [then] toast width stays within viewport third [else stop].
test('a long error toast width stays within one third of the viewport', async ({ page }) => {
	const longMessage =
		'selection load failed: ' + 'x'.repeat(400);
	const id = await raise(page, longMessage, 'error', 60_000);
	const width = await page.locator(`[data-toast-id="${id}"]`).evaluate((node) => node.getBoundingClientRect().width);
	const viewport = page.viewportSize()?.width ?? 1280;
	expect(width).toBeLessThanOrEqual(viewport / 3 + 4);
});
