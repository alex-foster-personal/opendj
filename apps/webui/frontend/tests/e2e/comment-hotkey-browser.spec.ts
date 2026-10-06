import { expect, test } from '@playwright/test';

/**
 * Exercises the real `m` hotkey against a REAL backend (r3918992947): the
 * prior version of this spec set `feedbackState.availability = 'ok'`
 * directly, fabricating the exact state the real availability probe in
 * feedback-store.svelte.ts is responsible for establishing - it could pass
 * even if hydration, API compatibility, or production mounting never makes
 * the hotkey usable in the shipped app.
 *
 * This version navigates to the real `/performance` route, which already
 * installs the real hotkey handler itself (src/routes/performance/+page.svelte
 * onMount -> installPerformanceHotkeys()) and mounts the real FeedbackWidget,
 * whose own onMount fires the real hydrateFeedback() probe against the real
 * backend booted by playwright.comment-hotkey-gate.config.ts. Nothing here
 * assigns store state directly; `placementArmed`/`availability` are only
 * ever read back to assert on what the real handlers already did.
 *
 * Runs under playwright.comment-hotkey-gate.config.ts (real backend, real
 * throwaway fixture library so the real
 * /api/v1/preflight library-attached check can pass at all), not the
 * default vite-only config.
 */

async function readFeedbackState(
	page: import('@playwright/test').Page
): Promise<{ availability: string; placementArmed: boolean }> {
	return page.evaluate(async () => {
		const mod = await import(new URL('/src/lib/rb/feedback-store.svelte.ts', location.href).href);
		return {
			availability: mod.feedbackState.availability,
			placementArmed: mod.feedbackState.placementArmed
		};
	});
}

/**
 * Seeds the per-viewer, localStorage-persisted comment-pin visibility
 * preference (FeedbackWidget.svelte's `pinsVisible`)
 * BEFORE the page's first script runs, via `addInitScript` - not after
 * `page.goto`, which would race the widget's own `onMount` read of the same
 * key. A fresh Playwright browser context always starts with empty
 * localStorage, so `pinsVisible` defaults to false (deliberately, for a
 * brand-new viewer) and FeedbackWidget's `pagePins` derived value - the only
 * thing `FeedbackPinMarkers.svelte` ever renders as `.fb-pin` - stays `[]`
 * forever, regardless of what pins the real backend holds. Any test that
 * asserts on a RENDERED `.fb-pin` marker (as opposed to `feedbackState.pins`
 * read directly off the store, which several tests in this file do and which
 * needs no visibility preference at all) must call this first. Uses the
 * app's own real key + serialization (`PINS_VISIBLE_KEY`,
 * `serializePinsVisible` in feedback.ts) via the same in-browser dynamic
 * import every other helper in this file uses, rather than hardcoding the
 * literal storage key/value here and risking silent drift if the app ever
 * changes either.
 */
async function seedPinsVisible(page: import('@playwright/test').Page): Promise<void> {
	await page.addInitScript(async () => {
		const mod = await import(new URL('/src/lib/rb/feedback.ts', location.href).href);
		window.localStorage.setItem(mod.PINS_VISIBLE_KEY, mod.serializePinsVisible(true));
	});
}

// The dev vite server compiles /performance's whole module graph on demand,
// on the FIRST request it ever receives - real, measured cost here, not a
// guess: with a warm cache every subsequent navigation's availability probe
// resolves in well under 10s, but the very first one in a freshly started
// worker can still be compiling when a 10s poll gives up (observed: blank
// page, zero DOM, at the 10s mark). A per-worker warm-up navigation here,
// with a generous timeout that only this one call needs, means every real
// test below - including the first - measures the real probe's own latency
// rather than the dev server's one-time compile cost.
test.beforeAll(async ({ browser }) => {
	// The hook's own timeout defaults to the config's per-test `timeout`
	// (30_000 here), which is what capped the 60_000ms poll below before this
	// line existed - the poll never actually got the time it asked for.
	test.setTimeout(90_000);
	const page = await browser.newPage();
	await page.goto('/performance');
	await expect
		.poll(async () => (await readFeedbackState(page)).availability, { timeout: 75_000 })
		.toBe('ok');
	await page.close();
});

// requirement: LIBM-171
// [if] /performance boots [then] it asks for feedback todos inside the boot window, [else stop].
// Feedback hydration is deferred boot work. After #5549 nothing released the
// deferred queue (22 failures on this gate); #5577 fixed the settle and added
// a hard backstop. The interval measured is from the scheduler's own start
// (the BOOT_ARMED_MARK performance mark) to the todos request, both on the
// page's clock. Measuring from navigation instead counted a loaded host's
// cold module compile (60-107 s on nucbox), which is not the property.
// Healthy: ~3.5 s. With the release removed: never, and this goes red.
const BOOT_ARMED_MARK = 'boot-scheduler:armed';
const BOOT_HARD_CEILING_MS = 14_000;
const CEILING_SLACK_MS = 3_000;
test('a /performance boot requests feedback todos within the ceiling of boot start', async ({ page }) => {
	test.setTimeout(180_000);
	const todos = page.waitForRequest(
		(request) => new URL(request.url()).pathname === '/api/v1/feedback/todos',
		{ timeout: 150_000 }
	);
	// The dev server serves hundreds of modules; the default 250-entry resource
	// buffer would drop the todos entry this reads.
	await page.addInitScript(() => performance.setResourceTimingBufferSize(20_000));
	await page.goto('/performance');
	const todosRequest = await todos;
	// waitForRequest resolves when the request STARTS; Chromium appends the
	// Resource Timing entry only when the response FINISHES. Reading the buffer
	// before then is a race the test, not the page, can lose: run 37484870161
	// read it 2 ms after the request began, 131 ms before a 133 ms response
	// ended. Waiting for the entry keeps the measured property unchanged.
	await (await todosRequest.response())?.finished();
	await page.waitForFunction(
		() =>
			performance
				.getEntriesByType('resource')
				.some((entry) => new URL(entry.name).pathname === '/api/v1/feedback/todos'),
		undefined,
		{ timeout: 10_000 }
	);
	const gapMs = await page.evaluate((markName) => {
		const armed = performance.getEntriesByName(markName)[0];
		const request = performance
			.getEntriesByType('resource')
			.find((entry) => new URL(entry.name).pathname === '/api/v1/feedback/todos');
		if (armed === undefined) throw new Error(`no ${markName} performance mark: the boot window never opened`);
		if (request === undefined) throw new Error('no resource timing entry for /api/v1/feedback/todos');
		return request.startTime - armed.startTime;
	}, BOOT_ARMED_MARK);
	expect(gapMs, 'feedback todos waited past the boot scheduler hard ceiling').toBeLessThan(
		BOOT_HARD_CEILING_MS + CEILING_SLACK_MS
	);
});

test.describe('comment hotkey, against a real backend', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/performance');
		// The real hydrateFeedback() probe is a real network round trip; wait
		// for it to actually resolve 'ok' rather than assuming a fixed delay.
		await expect
			.poll(async () => (await readFeedbackState(page)).availability, { timeout: 10_000 })
			.toBe('ok');
	});

	test('a real "m" keydown arms pin placement', async ({ page }) => {
		expect((await readFeedbackState(page)).placementArmed).toBe(false);
		await page.keyboard.press('m');
		expect((await readFeedbackState(page)).placementArmed).toBe(true);
	});

	test('typing "m" into a real focused text field does not arm placement', async ({ page }) => {
		await page.evaluate(() => {
			const input = document.createElement('input');
			input.id = 'probe-input';
			document.body.appendChild(input);
			input.focus();
		});
		await expect(page.locator('#probe-input')).toBeFocused();
		await page.keyboard.press('m');
		expect((await readFeedbackState(page)).placementArmed).toBe(false);
	});

	test('a modifier-held "m" does not arm placement', async ({ page }) => {
		await page.keyboard.press('Control+m');
		expect((await readFeedbackState(page)).placementArmed).toBe(false);
	});

	/**
	 * Regression target: a focused canvas[aria-label="deck 1 waveform seek"].
	 *
	 * The waveform seek canvas (WaveRow.svelte) is a pointer-only control -
	 * it has no onkeydown handler at all, so it never owns any keyboard
	 * behavior of its own. But it carries `role="slider"` and `tabindex="-1"`
	 * (for pointer-capture purposes only), which made the old native-interactive
	 * guard treat it as a native keyboard-owning
	 * widget the moment a click on it moved DOM focus there - its own
	 * onPointerDown skips preventDefault whenever the deck is empty or a
	 * command is pending, which is exactly when a click lets the browser's
	 * default focus-on-click behavior land on a tabindex="-1" element. Every
	 * performance hotkey, including 'm', silently stopped firing until focus
	 * moved elsewhere. A pointer seek does not make the canvas a text-entry
	 * widget.
	 *
	 * Fixed with an explicit `data-hotkey-pointer-only` opt-out on this one
	 * canvas, NOT by treating every negative tabindex as non-interactive -
	 * a P1 BLOCKING review caught that broader version swallowing keystrokes
	 * on any programmatically-focused input/button/contenteditable/ARIA
	 * widget with tabindex="-1" (modal focus traps, roving-tabindex menus).
	 */
	test('a real "m" keydown arms pin placement after clicking the waveform seek canvas', async ({
		page
	}) => {
		const waveform = page.locator('canvas[aria-label="deck 1 waveform seek"]');
		await waveform.click({ force: true });
		expect((await readFeedbackState(page)).placementArmed).toBe(false);
		await page.keyboard.press('m');
		expect((await readFeedbackState(page)).placementArmed).toBe(true);
	});
});

/**
 * Issue #3528: global shortcuts must fire outside text entry, including when
 * comment pins are hidden and when the library row or panel has focus/hover.
 */
test.describe('global shortcuts outside text entry (issue #3528)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/performance');
		await expect
			.poll(async () => (await readFeedbackState(page)).availability, { timeout: 10_000 })
			.toBe('ok');
	});

	async function readPinsVisibleTwin(page: import('@playwright/test').Page): Promise<boolean | null> {
		return page.evaluate(() => {
			const twin = (window as unknown as Record<string, unknown>).__mdtPinsVisible as
				| { get?: () => boolean }
				| undefined;
			return twin?.get?.() ?? null;
		});
	}

	/**
	 * Hover coverage is separate from keyboard focus: hover must
	 * never itself move keyboard focus onto (or off of) anything, so this
	 * confirms the hover is real (CSS `:hover` present) and that focus stays
	 * off text entry throughout, rather than assuming Playwright's `.hover()`
	 * has no focus side effect.
	 */
	test('hovering a library row (no text focus) then pressing M opens the pin composer', async ({
		page
	}) => {
		const firstRow = page.locator('[data-testid="track-row"]').first();
		await expect(firstRow).toBeVisible({ timeout: 30_000 });
		await firstRow.hover();
		// The real subject: hover is a POINTER-only state that must not itself
		// move keyboard focus onto (or off of) anything - Playwright's own
		// :hover pseudo-class check, not a paint-based proxy for it.
		expect(await firstRow.evaluate((el) => el.matches(':hover'))).toBe(true);
		expect(
			await page.evaluate(() => {
				const el = document.activeElement;
				return el === null || el === document.body ? 'inert' : el.tagName;
			})
		).not.toBe('INPUT');
		expect((await readFeedbackState(page)).placementArmed).toBe(false);

		await page.keyboard.press('m');

		expect((await readFeedbackState(page)).placementArmed).toBe(true);
		await expect(page.locator('.fb-place-overlay')).toBeVisible();
	});

	/**
	 * r3549 review P1 BLOCKING: the prior version of this test started with an
	 * empty fixture and no pin of its own, so `.fb-pin` reading 0 and the
	 * attachment-request list reading empty were true no matter what the
	 * pinsVisible preference did - deleting `_revealPinsIfHidden` entirely
	 * would still have left it green. This version creates a REAL pin
	 * (through the same `addPin` POST the viewport-resize and repeat-safe-save
	 * tests below use), asserts the preference is hidden through the real
	 * `__mdtPinsVisible` twin before touching it (a positive control on the
	 * precondition, not an assumed default), asserts that pin's own marker
	 * and its attachment fetch are both absent while hidden, then asserts the
	 * marker actually RENDERS - not just that the twin's internal flag
	 * flipped - once `M` reveals it.
	 */
	test('hidden pins stay off the canvas until M reveals them and opens placement', async ({
		page
	}) => {
		const attachmentRequests: string[] = [];
		page.on('request', (req) => {
			if (req.url().includes('/api/v1/feedback/comments/') && req.url().includes('/attachment')) {
				attachmentRequests.push(req.url());
			}
		});

		expect(await readPinsVisibleTwin(page), 'a fresh context must default to hidden pins').toBe(false);

		const viewport = page.viewportSize() ?? { width: 1280, height: 720 };
		await page.evaluate(
			async ({ width, height }) => {
				const mod = await import(new URL('/src/lib/rb/feedback-store.svelte.ts', location.href).href);
				await mod.addPin({
					x_pct: 50,
					y_pct: 50,
					anchor: null,
					page: '/performance',
					text: 'hidden-pin-reveal probe',
					ui: 'chrome-loop',
					viewport_width: width,
					viewport_height: height
				});
			},
			{ width: viewport.width, height: viewport.height }
		);
		// Scoped to the pin this test just created - other tests in this suite
		// share the same backend data dir (see the archived-pin test's own note).
		const marker = page.locator('.fb-pin[title^="hidden-pin-reveal probe"]');

		await expect(marker).toHaveCount(0);
		expect(attachmentRequests).toEqual([]);

		const browserPanel = page.getByTestId('browser-panel');
		await browserPanel.hover();
		await page.keyboard.press('m');

		expect((await readFeedbackState(page)).placementArmed).toBe(true);
		await expect(page.locator('.fb-place-overlay')).toBeVisible();
		await expect.poll(() => readPinsVisibleTwin(page)).toBe(true);
		await expect(marker).toHaveCount(1);

		await page.locator('.fb-place-overlay').click({ position: { x: 80, y: 80 } });
		await expect(page.locator('.fb-bubble-text')).toBeVisible();
	});

	/**
	 * The topbar "Show feedback comment pins" checkbox, the M reveal and the
	 * agent twin all write one preference (FB-17). Before this was wired, the
	 * widget read it only on mount, so an M reveal or a twin write left the
	 * checkbox showing the old value and the next click wrote the value already
	 * stored - a no-op. The click-through control keeps the original UI -> twin
	 * direction honest.
	 */
	test('the topbar pins checkbox follows the M reveal and twin writes both ways, and its own click still reaches the twin', async ({
		page
	}) => {
		expect(await readPinsVisibleTwin(page), 'a fresh context must default to hidden pins').toBe(false);

		// The topbar's own pin button: FB-18c's always-on dock (#3981) carries a
		// second one with the same name, and the checkbox under test lives in
		// the topbar's explainer, not the dock's.
		const pinButton = page.getByRole('banner').getByRole('button', { name: 'Drop a comment pin' });
		const checkbox = page.getByRole('checkbox', { name: 'Show feedback comment pins' });
		await pinButton.hover();
		await expect(checkbox).not.toBeChecked();

		// The M reveal is a third writer of the same preference.
		await page.getByTestId('browser-panel').hover();
		await page.keyboard.press('m');
		await expect(page.locator('.fb-place-overlay')).toBeVisible();
		await page.keyboard.press('Escape');
		await expect(page.locator('.fb-place-overlay')).toHaveCount(0);
		await pinButton.hover();
		await expect(checkbox).toBeChecked();

		const setTwin = (value: boolean) =>
			page.evaluate((v) => {
				const twin = (window as unknown as Record<string, unknown>).__mdtPinsVisible as {
					set: (next: boolean) => void;
				};
				twin.set(v);
			}, value);

		await setTwin(false);
		await expect(checkbox).not.toBeChecked();
		await setTwin(true);
		await expect(checkbox).toBeChecked();
		await setTwin(false);
		await expect(checkbox).not.toBeChecked();

		await checkbox.click();
		await expect.poll(() => readPinsVisibleTwin(page)).toBe(true);
		await expect(checkbox).toBeChecked();
	});

	test('Space on a library row toggles transport instead of scrolling the page', async ({ page }) => {
		test.setTimeout(90_000);
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
			timeout: 30_000
		});
		const firstRow = page.locator('[data-testid="track-row"]').first();
		await expect(firstRow).toBeVisible({ timeout: 30_000 });
		await firstRow.locator('td.c-title').dblclick();
		const confirmYes = page.locator('.load-confirm[role="dialog"] .load-confirm-yes');
		if (await confirmYes.isVisible()) await confirmYes.click();
		await page.waitForFunction(
			() => window.musicDjToolsPerformance?.query().decks[1].stable_id !== null,
			undefined,
			{ timeout: 45_000 }
		);
		// The confirm dialog's "Yes" is a Load+Play confirmation - the deck is
		// already playing once this resolves, not paused. Found live running
		// this suite: the wait below used to assert playing === false and
		// timed out every run, unrelated to anything issue #3528 touches.
		await page.waitForFunction(
			() => window.musicDjToolsPerformance?.query().decks[1].playing === true,
			undefined,
			{ timeout: 10_000 }
		);

		await firstRow.focus();
		// review r3549 P3: Space's browser default scrolls the focused
		// element's nearest scrollable ancestor - for a track row that is the
		// library's own `.table-wrap` container, not the document, so
		// window.scrollX/Y would read unchanged even with preventDefault()
		// removed. Assert focus actually landed on the row (the real subject
		// of this test) and measure the container that would actually move.
		await expect(firstRow).toBeFocused();
		const scrollContainerBefore = await page.evaluate(
			() => document.querySelector('.table-wrap')?.scrollTop ?? null
		);
		await page.keyboard.press('Space');
		const scrollContainerAfter = await page.evaluate(
			() => document.querySelector('.table-wrap')?.scrollTop ?? null
		);
		expect(scrollContainerAfter).toEqual(scrollContainerBefore);
		// Space runs _toggleRecentPlay, a TOGGLE - the deck was already
		// playing (Load+Play confirm above), so the real app action here is
		// pausing it, not starting it.
		await page.waitForFunction(
			() => window.musicDjToolsPerformance?.query().decks[1].playing === false,
			undefined,
			{ timeout: 10_000 }
		);
	});

	test('Space inside a text input is left alone', async ({ page }) => {
		await page.evaluate(() => {
			const input = document.createElement('input');
			input.id = 'space-probe-input';
			document.body.appendChild(input);
			input.focus();
		});
		await expect(page.locator('#space-probe-input')).toBeFocused();
		await page.keyboard.press('Space');
		await expect(page.locator('#space-probe-input')).toHaveValue(' ');
	});
});

/**
 * r3919185341: a parked pin draft carries no page of its own, so restoring
 * one after navigating elsewhere and saving from there silently reattaches
 * it to the WRONG page with stale coordinates. feedback.ts's PinDraft now
 * carries `page`, captured at creation and checked against the live
 * pathname on restore (FeedbackWidget.svelte onMount). This drives the
 * real restore path with a real localStorage value and a real page load -
 * only the STORED page tag is hand-set, which is exactly what a real
 * cross-page navigation would have left behind.
 */
test.describe('pin draft restore, page-tagged', () => {
	test('a draft tagged to a different page is dropped, not restored', async ({ page }) => {
		await page.goto('/performance');
		await page.evaluate(() => {
			localStorage.setItem(
				'odj-feedback-pin-draft',
				JSON.stringify({
					point: { x_pct: 40, y_pct: 40 },
					anchor: null,
					text: 'stale, from another page',
					page: '/performance/preload1'
				})
			);
		});
		await page.reload();
		await expect(page.locator('.fb-bubble')).toHaveCount(0);
	});

	test('a draft tagged to the current page IS restored', async ({ page }) => {
		await page.goto('/performance');
		await page.evaluate(() => {
			localStorage.setItem(
				'odj-feedback-pin-draft',
				JSON.stringify({
					point: { x_pct: 40, y_pct: 40 },
					anchor: null,
					text: 'still here',
					page: '/performance'
				})
			);
		});
		await page.reload();
		await expect(page.locator('.fb-bubble')).toHaveCount(1);
		await expect(page.locator('.fb-bubble textarea')).toHaveValue('still here');
	});

	// r3919460207: the page-mismatch drop above left pinDraft null in local
	// state, and the persistence $effect used to read that null the same way
	// it reads a user-cleared draft - deleting the very copy still parked in
	// storage for its own page. Visiting any other page even briefly used to
	// permanently destroy an in-progress draft, which is strictly worse than
	// what r3919185341 set out to fix (stop it being wrongly SHOWN here, not
	// make it vanish everywhere).
	test('a draft tagged to a different page survives being left un-restored, not deleted', async ({
		page
	}) => {
		await page.goto('/performance');
		const stored = JSON.stringify({
			point: { x_pct: 40, y_pct: 40 },
			anchor: null,
			text: 'still mine, wrong page to view it here',
			page: '/performance/preload1'
		});
		await page.evaluate((value) => localStorage.setItem('odj-feedback-pin-draft', value), stored);
		await page.reload();
		await expect(page.locator('.fb-bubble')).toHaveCount(0);
		// The persistence effect flushes in the same onMount pass that starts
		// the real hydrateFeedback() probe - waiting for that probe to resolve
		// proves the effect has already run at least once on this load.
		await expect
			.poll(async () => (await readFeedbackState(page)).availability, { timeout: 10_000 })
			.toBe('ok');
		expect(await page.evaluate(() => localStorage.getItem('odj-feedback-pin-draft'))).toBe(stored);
	});
});

/**
 * r3919227524: the pin-card measurement effect only reran when `openPin` or
 * `cardEl` changed, not on a viewport resize - a card whose real height
 * grows once `max-height: 46vh` has more room (window enlarged) kept
 * clamping against its pre-resize height, letting the real card's bottom
 * sit past the viewport. Drives this against a real pin (created through
 * the real POST /api/v1/feedback/comments, not fabricated store state), a
 * real open card, and a real `page.setViewportSize` + real bounding boxes.
 */
test.describe('pin card remeasures after a real viewport resize', () => {
	test('an open card stays inside a real, enlarged viewport', async ({ page }) => {
		await seedPinsVisible(page);
		await page.goto('/performance');
		await expect
			.poll(async () => (await readFeedbackState(page)).availability, { timeout: 10_000 })
			.toBe('ok');

		// A long pin, placed low, in a SHORT starting viewport: the card's own
		// 46vh cap keeps it short here, and its natural (uncapped) height is
		// taller than that cap - exactly the shape that has more room to grow
		// once the viewport enlarges.
		await page.setViewportSize({ width: 1000, height: 500 });
		const longText = Array.from({ length: 40 }, (_, i) => `line ${i} of a long pin`).join('\n');
		// `ui`/`viewport_width`/`viewport_height` are required by
		// CommentCreateIn (apps/webui/server/routes/feedback.py) since #1290
		// (feat(feedback): capture comment pin environment); this direct
		// store call predates that and used to 422 with all three reported
		// missing. Values match what the real Save-button path sends
		// (FeedbackPinDraftBubble.svelte's own `pinUiKind()` + viewport dims),
		// not an arbitrary literal that merely satisfies the validator.
		await page.evaluate(async ({ text, width, height }) => {
			const mod = await import(new URL('/src/lib/rb/feedback-store.svelte.ts', location.href).href);
			await mod.addPin({
				x_pct: 50,
				y_pct: 80,
				anchor: null,
				page: '/performance',
				text,
				ui: 'chrome-loop',
				viewport_width: width,
				viewport_height: height
			});
		}, { text: longText, width: 1000, height: 500 });

		// Scoped to the pin this test just created - other tests in this suite
		// share the same backend data dir (see the archived-pin test's own
		// note; found live when the #3528 hidden-pin-reveal test's own real
		// pin leaked into this file's generic `.fb-pin` locator, which then
		// matched two elements instead of one).
		const longPin = page.locator('.fb-pin[title^="line 0 of a long pin"]');
		// A zero-gap mousedown/mouseup pair is unreliable against this
		// button's real onclick handler in Chromium (observed directly:
		// dispatching a synthetic click or adding a short down/up gap both
		// register, a 0ms gap intermittently does not) - delay is Playwright's
		// own documented knob for exactly this class of flake.
		await longPin.click({ delay: 50 });
		await expect(page.locator('.fb-pin-body')).toBeVisible();

		// Enlarge the viewport with the card still open: 46vh now permits a
		// much taller card than the pre-resize measurement clamped it to.
		await page.setViewportSize({ width: 1000, height: 1400 });
		await expect
			.poll(async () => {
				const [card, viewport] = await Promise.all([
					page.locator('.fb-pin-body').boundingBox(),
					page.evaluate(() => ({ w: window.innerWidth, h: window.innerHeight }))
				]);
				if (card === null) return null;
				return card.y + card.height <= viewport.h;
			})
			.toBe(true);
	});
});

/**
 * r3919761150: while the textarea has focus, holding Cmd/Ctrl+Enter's real
 * OS key-repeat re-invokes savePinDraft() on every keydown while pinDraft
 * stays non-null until the first POST resolves - each invocation could
 * create a duplicate pin. Drives the real placement UI (arm, click, type,
 * two real keydowns back to back) against the real POST
 * /api/v1/feedback/comments this gate's backend serves, and reads the real
 * feedbackState.pins the app itself renders from - not a mock, not a direct
 * addPin() call standing in for the UI.
 */
test.describe('pin draft save, repeat-safe', () => {
	test('two rapid Cmd/Ctrl+Enter presses create exactly one real pin', async ({ page }) => {
		await page.goto('/performance');
		await expect
			.poll(async () => (await readFeedbackState(page)).availability, { timeout: 10_000 })
			.toBe('ok');

		await page.keyboard.press('m');
		await page.locator('.fb-place-overlay').click({ position: { x: 60, y: 60 } });
		await page.locator('.fb-bubble-text').fill('rapid-save repeat guard');

		const countPins = () =>
			page.evaluate(async () => {
				const mod = await import(new URL('/src/lib/rb/feedback-store.svelte.ts', location.href).href);
				return mod.feedbackState.pins.length;
			});
		const before = await countPins();

		// Real OS key-repeat sends the same keydown multiple times before the
		// first one's async work resolves - two presses with no artificial
		// gap between them is the same race, not a simulation of it.
		await page.keyboard.press('Control+Enter');
		await page.keyboard.press('Control+Enter');

		await expect(page.locator('.fb-bubble')).toHaveCount(0, { timeout: 10_000 });
		await expect.poll(countPins, { timeout: 10_000 }).toBe(before + 1);
		// Give a genuine duplicate POST time to land before declaring victory.
		await page.waitForTimeout(500);
		expect(await countPins()).toBe(before + 1);
	});

	/**
	 * A single Meta+Enter press must save, independently of Control+Enter.
	 * The handler treats both modifiers identically; the repeated Control
	 * test above does not exercise the Meta chord or a single press.
	 * This drives that chord against the real placement UI, textarea and
	 * backend without synthesizing a metaKey flag.
	 */
	test('a real Meta+Enter (Cmd) keydown saves the comment being typed', async ({ page }) => {
		await page.goto('/performance');
		await expect
			.poll(async () => (await readFeedbackState(page)).availability, { timeout: 10_000 })
			.toBe('ok');

		const countPins = () =>
			page.evaluate(async () => {
				const mod = await import(new URL('/src/lib/rb/feedback-store.svelte.ts', location.href).href);
				return mod.feedbackState.pins.length;
			});
		const before = await countPins();

		await page.keyboard.press('m');
		await page.locator('.fb-place-overlay').click({ position: { x: 60, y: 60 } });
		await page.locator('.fb-bubble-text').fill('meta-enter save');

		await page.keyboard.press('Meta+Enter');

		await expect(page.locator('.fb-bubble')).toHaveCount(0, { timeout: 10_000 });
		await expect.poll(countPins, { timeout: 10_000 }).toBe(before + 1);
	});

	/**
	 * Diagnostic variant of the test directly above, using natural
	 * interaction than `.fill()` (which sets the value in one programmatic
	 * step): real per-character keystrokes via `pressSequentially`, then a
	 * pause (a real person reads back what they typed before hitting save),
	 * with an explicit `document.activeElement` check immediately before the
	 * Meta+Enter press - so if focus silently left the textarea between
	 * typing and saving, this fails ON THE ASSERTION
	 * THAT NAMES IT, not on the save outcome three steps later.
	 */
	test('a real Meta+Enter still saves after natural typing and a pause, focus verified first', async ({
		page
	}) => {
		await page.goto('/performance');
		await expect
			.poll(async () => (await readFeedbackState(page)).availability, { timeout: 10_000 })
			.toBe('ok');

		const countPins = () =>
			page.evaluate(async () => {
				const mod = await import(new URL('/src/lib/rb/feedback-store.svelte.ts', location.href).href);
				return mod.feedbackState.pins.length;
			});
		const before = await countPins();

		await page.keyboard.press('m');
		await page.locator('.fb-place-overlay').click({ position: { x: 60, y: 60 } });
		await page
			.locator('.fb-bubble-text')
			.pressSequentially('meta-enter save, typed naturally', { delay: 25 });

		// A real person pauses to read back what they typed before saving.
		await page.waitForTimeout(400);

		const activeElementClass = await page.evaluate(
			() => (document.activeElement as HTMLElement | null)?.className ?? null
		);
		expect(activeElementClass).toContain('fb-bubble-text');

		await page.keyboard.press('Meta+Enter');

		await expect(page.locator('.fb-bubble')).toHaveCount(0, { timeout: 10_000 });
		await expect.poll(countPins, { timeout: 10_000 }).toBe(before + 1);
	});

	/**
	 * r3919867508: the textarea stays editable while a save is in flight, so
	 * text typed after Save was clicked used to be silently discarded the
	 * moment a success response arrived, since the handler cleared pinDraft
	 * unconditionally. `page.route` here only delays delivery of the real
	 * response (the real POST still reaches the real backend and is really
	 * persisted) so there is a real window to type into while it is pending -
	 * not a fabricated response standing in for the backend.
	 */
	test('text typed while a save is still in flight is not discarded when it resolves', async ({
		page
	}) => {
		await page.goto('/performance');
		await expect
			.poll(async () => (await readFeedbackState(page)).availability, { timeout: 10_000 })
			.toBe('ok');

		await page.keyboard.press('m');
		await page.locator('.fb-place-overlay').click({ position: { x: 60, y: 60 } });
		await page.locator('.fb-bubble-text').fill('first part');

		await page.route('**/api/v1/feedback/comments', async (route) => {
			await new Promise((resolve) => setTimeout(resolve, 1000));
			await route.continue();
		});

		await page.locator('.fb-mini', { hasText: 'Save pin' }).click();
		await page.locator('.fb-bubble-text').pressSequentially(' and more', { delay: 20 });

		// The in-flight save must not clobber the newer text once it resolves.
		await page.waitForTimeout(1500);
		await expect(page.locator('.fb-bubble')).toHaveCount(1);
		await expect(page.locator('.fb-bubble-text')).toHaveValue('first part and more');
	});

	/**
	 * r3920118985: comparing only trimmed text meant a cancel + re-place with
	 * the SAME text under an in-flight save got wrongly cleared when the old
	 * save resolved, even though it belongs to a different draft (different
	 * coordinates) - a real scenario when correcting a pin's placement.
	 * Captures the exact draft object at submit time and only clears if that
	 * same object is still the active one, driven through the real UI (cancel
	 * button, real re-placement click) against the real, delayed POST.
	 */
	test('cancelling and re-placing with the same text survives the old save resolving', async ({
		page
	}) => {
		await page.goto('/performance');
		await expect
			.poll(async () => (await readFeedbackState(page)).availability, { timeout: 10_000 })
			.toBe('ok');

		const countPins = () =>
			page.evaluate(async () => {
				const mod = await import(new URL('/src/lib/rb/feedback-store.svelte.ts', location.href).href);
				return mod.feedbackState.pins.length;
			});
		const before = await countPins();

		await page.route('**/api/v1/feedback/comments', async (route) => {
			await new Promise((resolve) => setTimeout(resolve, 1000));
			await route.continue();
		});

		await page.keyboard.press('m');
		await page.locator('.fb-place-overlay').click({ position: { x: 60, y: 60 } });
		await page.locator('.fb-bubble-text').fill('same wording');
		await page.locator('.fb-mini', { hasText: 'Save pin' }).click();

		// Cancel the in-flight draft and re-place at a different spot with the
		// exact same text - a real correction workflow, not a contrived race.
		await page.locator('.fb-mini', { hasText: 'Cancel' }).click();
		await expect(page.locator('.fb-bubble')).toHaveCount(0);
		await page.keyboard.press('m');
		await page.locator('.fb-place-overlay').click({ position: { x: 200, y: 150 } });
		await page.locator('.fb-bubble-text').fill('same wording');

		// The original save resolves in the background; it must not clear the
		// new draft just because the text happens to match.
		await page.waitForTimeout(1500);
		await expect(page.locator('.fb-bubble')).toHaveCount(1);
		await expect(page.locator('.fb-bubble-text')).toHaveValue('same wording');
		await expect.poll(countPins, { timeout: 10_000 }).toBe(before + 1);
	});
});

/**
 * r3920038864, updated per r3921927395: PATCH only ever accepts
 * open/issued/fixed/merged (apps/webui/server/routes/feedback_pins.py's
 * `_PATCHABLE_STATUSES`) - archiving is a dedicated
 * `POST /comments/{id}/archive`, gated on the pin already being
 * fixed/merged, so a plain PATCH cannot make a pin vanish from the board
 * with no archive-file entry. isPinDrawn() (feedback.ts) then filters
 * `archived` pins out of the rendered marker list entirely, rather than
 * styling them differently. Drives a real POST to create the pin, a real
 * PATCH to fixed (the archive endpoint's own precondition), a real POST to
 * archive it (the same endpoints an external agent workflow uses, since
 * there is no in-app control for either), and a real reload so
 * hydrateFeedback() re-fetches the archived status from the real backend -
 * not a fabricated store mutation.
 */
test.describe('archived pin status styling', () => {
	test('a pin archived through the real lifecycle endpoints leaves the canvas', async ({
		page
	}) => {
		// Without this, the pin this test creates through the real UI is
		// created and persisted fine (a real 201, confirmed present on the
		// very next GET) but never renders as `.fb-pin` at all: marker
		// rendering is gated behind the same per-viewer pinsVisible
		// preference seedPinsVisible sets, which defaults off for a fresh
		// browser context - see that helper's own comment.
		await seedPinsVisible(page);
		await page.goto('/performance');
		await expect
			.poll(async () => (await readFeedbackState(page)).availability, { timeout: 10_000 })
			.toBe('ok');

		await page.keyboard.press('m');
		await page.locator('.fb-place-overlay').click({ position: { x: 60, y: 60 } });
		await page.locator('.fb-bubble-text').fill('archive me');
		await page.locator('.fb-mini', { hasText: 'Save pin' }).click();
		await expect(page.locator('.fb-bubble')).toHaveCount(0, { timeout: 10_000 });

		const pinId: string = await page.evaluate(async () => {
			const mod = await import(new URL('/src/lib/rb/feedback-store.svelte.ts', location.href).href);
			return mod.feedbackState.pins.at(-1).id;
		});
		// Scoped to the one pin this test just created: other tests in this
		// suite share the same backend data dir and leave their own pins
		// behind, so a marker count or a bare `.fb-pin` selector would be
		// polluted by them. The marker's title always starts with the pin's
		// own text, which nothing else in this run shares.
		const archivedPin = page.locator('.fb-pin[title^="archive me"]');
		await expect(archivedPin).toHaveCount(1);

		// The archive endpoint 409s unless the pin is already fixed/merged
		// (feedback_pins.py's archive_comment), so it must be marked done
		// first - the same precondition the in-app Archive button enforces.
		const patchStatus = await page.evaluate(async (id: string) => {
			const res = await fetch(`/api/v1/feedback/comments/${id}`, {
				method: 'PATCH',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ status: 'fixed' })
			});
			return res.status;
		}, pinId);
		expect(patchStatus).toBe(200);

		const archiveStatus = await page.evaluate(async (id: string) => {
			const res = await fetch(`/api/v1/feedback/comments/${id}/archive`, { method: 'POST' });
			return res.status;
		}, pinId);
		expect(archiveStatus).toBe(200);

		await page.reload();
		await expect
			.poll(async () => (await readFeedbackState(page)).availability, { timeout: 10_000 })
			.toBe('ok');
		// isPinDrawn() filters `archived` pins out of pagePins (feedback.ts),
		// so the merged lifecycle implementation never renders a marker for
		// this pin at all - it must leave the canvas, not merely change class.
		await expect(archivedPin).toHaveCount(0);
	});
});

/**
 * r3919227515: tests/unit/app-init.test.mjs's "the root layout actually
 * calls startAppInstruments" only regex-matches +layout.svelte's source
 * text - it stays green even if the matched call were unreachable (dead
 * code) or sat inside a comment, and it proves nothing about a REAL page
 * actually mounting a working announcer. This is that proof: a normally
 * mounted real page, no dynamic import of the installer, no fake globals -
 * if the root layout's real onMount ever stops calling startAppInstruments,
 * window.__mdtScheduleReload never appears and this fails where the regex
 * could not.
 */
test.describe('root layout wiring, on a real mounted page', () => {
	test('a real page load installs window.__mdtScheduleReload without any test-side call', async ({
		page
	}) => {
		await page.goto('/performance');
		await expect
			.poll(
				() => page.evaluate(() => typeof (window as unknown as Record<string, unknown>).__mdtScheduleReload),
				{ timeout: 10_000 }
			)
			.toBe('function');
	});
});

/**
 * Issue #698: right-click a loaded wavestack row, choose Fix / add comment,
 * and drop a real FB-03 pin whose anchor starts with `vocal-area:`. Region
 * encoding is unit-tested; this drives the real menu + real POST even when
 * the fixture track is not_analyzed. Does not seed a fake vocal-cache.
 */
test.describe('vocal-area Fix/add comment (issue #698)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/performance');
		await expect
			.poll(async () => (await readFeedbackState(page)).availability, { timeout: 10_000 })
			.toBe('ok');
	});

	test('empty deck omits Fix / add comment', async ({ page }) => {
		const waveform = page.locator('canvas[aria-label="deck 1 waveform seek"]');
		await waveform.click({ button: 'right', force: true });
		await expect(page.getByTestId('quick-draw-menu')).toBeVisible();
		await expect(page.getByTestId('quick-draw-vocal-fix')).toHaveCount(0);
	});

	test('loaded wavestack opens a vocal-area pin draft and POSTs it', async ({ page }) => {
		test.setTimeout(90_000);
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
			timeout: 30_000
		});
		const firstRow = page.locator('[data-testid="track-row"]').first();
		await expect(firstRow).toBeVisible({ timeout: 30_000 });
		const stableId = await firstRow.getAttribute('data-stable-id');
		if (stableId === null) throw new Error('fixture row has no data-stable-id');

		await firstRow.locator('td.c-title').dblclick();
		const confirmYes = page.locator('.load-confirm[role="dialog"] .load-confirm-yes');
		if (await confirmYes.isVisible()) await confirmYes.click();
		await page.waitForFunction(
			() => window.musicDjToolsPerformance?.query().decks[1].stable_id !== null,
			undefined,
			{ timeout: 45_000 }
		);
		await expect(page.locator('section.rb-deck[data-deck="1"] .title')).not.toHaveText(
			'No track loaded'
		);

		const countPins = () =>
			page.evaluate(async () => {
				const mod = await import(new URL('/src/lib/rb/feedback-store.svelte.ts', location.href).href);
				return mod.feedbackState.pins.length;
			});
		const before = await countPins();

		const waveform = page.locator('canvas[aria-label="deck 1 waveform seek"]');
		await waveform.click({ button: 'right', force: true });
		await expect(page.getByTestId('quick-draw-vocal-fix')).toBeVisible();
		await page.getByTestId('quick-draw-vocal-fix').click();

		await expect(page.locator('.fb-bubble')).toBeVisible();
		await expect(page.locator('.fb-bubble-text')).toHaveValue(/Incorrect vocal area/);
		await expect(page.locator('.fb-bubble-text')).toHaveValue(new RegExp(stableId));

		await page.locator('.fb-mini', { hasText: 'Save pin' }).click();
		await expect(page.locator('.fb-bubble')).toHaveCount(0, { timeout: 10_000 });
		await expect.poll(countPins, { timeout: 10_000 }).toBe(before + 1);

		const res = await page.request.get('/api/v1/feedback/comments');
		expect(res.ok(), 'GET /api/v1/feedback/comments must succeed').toBeTruthy();
		const body = (await res.json()) as {
			comments: Array<{ text: string; anchor: string | null; page: string }>;
		};
		const created = body.comments.find(
			(c) =>
				typeof c.anchor === 'string' &&
				c.anchor.startsWith('vocal-area:') &&
				c.anchor.includes(stableId)
		);
		expect(created, 'GET /api/v1/feedback/comments must contain the vocal-area pin').toBeTruthy();
		expect(created?.anchor).toMatch(/^vocal-area:/);
		expect(created?.page).toBe('/performance');
	});
});

test.describe('comment hotkey on library shell (issue #3980)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/');
		await expect
			.poll(async () => (await readFeedbackState(page)).availability, { timeout: 30_000 })
			.toBe('ok');
	});

	test('M arms placement on the library route', async ({ page }) => {
		expect((await readFeedbackState(page)).placementArmed).toBe(false);
		await page.keyboard.press('m');
		expect((await readFeedbackState(page)).placementArmed).toBe(true);
		await expect(page.locator('.fb-place-overlay')).toBeVisible();
	});

	test('Meta+Shift+M arms placement from a focused text field on library', async ({ page }) => {
		await page.evaluate(() => {
			const input = document.createElement('input');
			input.id = 'library-probe-input';
			document.body.appendChild(input);
			input.focus();
		});
		await expect(page.locator('#library-probe-input')).toBeFocused();
		await page.keyboard.press('Meta+Shift+M');
		expect((await readFeedbackState(page)).placementArmed).toBe(true);
	});
});
