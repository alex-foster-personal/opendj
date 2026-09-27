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

/** Push a toast with the real store after arming a tray MutationObserver for `data-toast-exiting`. */
async function raiseAndObserveExitMarker(
	page: Page,
	message: string,
	kind: 'info' | 'warn' | 'error' = 'error',
	dismissMs = 120_000
): Promise<string> {
	return page.evaluate(
		async ([store, msg, k, ms]) => {
			const tray = document.querySelector('.toast-stack');
			if (!tray) {
				throw new Error('toast-tray e2e: .toast-stack not found before push');
			}

			return new Promise<string>((resolve, reject) => {
				const timeoutMs = 5000;
				let settled = false;
				let observer: MutationObserver;

				const finish = (exitId: string) => {
					if (settled) return;
					settled = true;
					observer.disconnect();
					clearTimeout(timer);
					resolve(exitId);
				};

				const fail = (reason: string) => {
					if (settled) return;
					settled = true;
					observer.disconnect();
					clearTimeout(timer);
					reject(new Error(reason));
				};

				const readExitId = (root: Node): string | null => {
					if (root.nodeType !== Node.ELEMENT_NODE) return null;
					const el = root as Element;
					const direct = el.getAttribute('data-toast-exiting');
					if (direct) return direct;
					const nested = el.querySelector('[data-toast-exiting]');
					return nested?.getAttribute('data-toast-exiting') ?? null;
				};

				const timer = setTimeout(() => {
					fail('toast-tray e2e: exit marker not observed');
				}, timeoutMs);

				observer = new MutationObserver((mutations) => {
					for (const mutation of mutations) {
						if (mutation.type === 'attributes' && mutation.attributeName === 'data-toast-exiting') {
							const val = (mutation.target as Element).getAttribute('data-toast-exiting');
							if (val) {
								finish(val);
								return;
							}
						}
						if (mutation.type === 'childList') {
							for (const node of mutation.addedNodes) {
								const val = readExitId(node);
								if (val) {
									finish(val);
									return;
								}
							}
						}
					}
				});

				observer.observe(tray, {
					subtree: true,
					childList: true,
					attributes: true,
					attributeFilter: ['data-toast-exiting']
				});

				void (async () => {
					try {
						const mod = await import(/* @vite-ignore */ store as string);
						mod.pushToast(msg as string, k as 'info' | 'warn' | 'error', ms as number);
					} catch (err) {
						fail(`toast-tray e2e: pushToast failed: ${String(err)}`);
					}
				})();
			});
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

test('at most three toasts are visible when more are pushed', async ({ page }) => {
	const ids: string[] = [];
	for (let i = 0; i < 5; i++) {
		ids.push(await raise(page, `stack toast ${i}`, 'error', 120_000));
	}
	await page.waitForTimeout(50);
	const visible = page.locator('[data-toast-id]');
	await expect(visible).toHaveCount(3, { timeout: 3000 });
});

test('the fourth toast evicts the oldest with an exiting marker', async ({ page }) => {
	const first = await raise(page, 'oldest toast', 'error', 120_000);
	await raise(page, 'toast two', 'error', 120_000);
	await raise(page, 'toast three', 'error', 120_000);
	const exitingId = await raiseAndObserveExitMarker(page, 'toast four', 'error', 120_000);
	expect(exitingId).toBe(first);
});

test('a long error toast width stays within one third of the viewport', async ({ page }) => {
	const longMessage =
		'selection load failed: ' + 'x'.repeat(400);
	const id = await raise(page, longMessage, 'error', 60_000);
	const width = await page.locator(`[data-toast-id="${id}"]`).evaluate((node) => node.getBoundingClientRect().width);
	const viewport = page.viewportSize()?.width ?? 1280;
	expect(width).toBeLessThanOrEqual(viewport / 3 + 4);
});
