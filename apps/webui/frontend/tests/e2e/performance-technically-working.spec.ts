import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';

/**
 * LIBUX-05 "Technically-working mode" keyboard + pointer wiring, real
 * browser throughout. `technically-working-hotkeys.ts` used to carry a
 * fake-window/fake-document unit test alongside this file; that fake event
 * target never ran the module against a real `window.addEventListener` or a
 * real `KeyboardEvent`, which is exactly what AGENTS.md's no-mocks rule
 * rules out ("TDD must begin against the real production code path"). This
 * file is the replacement: every case below drives the real installed
 * listeners via Playwright, reading state back through the same typed
 * `window.musicDjToolsPerformance` IPC surface a browser agent would use
 * (agent-native parity - see performance-ipc.svelte.ts).
 */

const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';

async function techState(page: Page) {
	return page.evaluate(() => window.musicDjToolsPerformance!.query().technically_working);
}

/** A real, on-disk library track's stable_id - no BPM/anlz requirement, since
 * isDeckSlotVisible only needs `decks[deck].stable_id !== null` to consider a
 * deck "loaded". */
async function realTrackStableId(request: APIRequestContext): Promise<string> {
	const response = await request.get(`${API_BASE}/api/v1/tracks?limit=50&available=true`);
	expect(response.ok(), 'real available-track listing must succeed').toBeTruthy();
	const payload = (await response.json()) as { items: Array<{ stable_id: string; file_exists: boolean }> };
	const track = payload.items.find((item) => item.file_exists);
	expect(track, 'real library must expose at least one on-disk track').toBeTruthy();
	return track!.stable_id;
}

test.beforeEach(async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
});

test('cmd+r toggles overlay mode, and top/bottom edges reveal on hover without any deck loaded', async ({
	page
}) => {
	const root = page.locator('.perf-root');
	await expect(root).not.toHaveClass(/tw-active/);

	await page.keyboard.down('Control');
	await page.keyboard.press('r');
	await page.keyboard.up('Control');
	await expect(root).toHaveClass(/tw-active/);

	// top/bottom are not deck-gated, so hovering there reveals with zero decks
	// loaded; left/right stay hidden (deck-gated, and no deck is loaded).
	await page.mouse.move(1000, 5);
	await expect(root).toHaveClass(/tw-top-visible/);
	await expect(root).not.toHaveClass(/tw-left-visible/);
	await expect(root).not.toHaveClass(/tw-right-visible/);

	await page.mouse.move(1000, 795);
	await expect(root).toHaveClass(/tw-bottom-visible/);

	// Opt reveals the homeless group (mixer/topbar) while held, never toggling mode.
	await page.keyboard.down('Alt');
	await expect(root).toHaveClass(/tw-homeless-visible/);
	await page.keyboard.up('Alt');
	await expect(root).not.toHaveClass(/tw-homeless-visible/);
	await expect(root).toHaveClass(/tw-active/);

	// cmd+r again toggles mode back off.
	await page.keyboard.down('Control');
	await page.keyboard.press('r');
	await page.keyboard.up('Control');
	await expect(root).not.toHaveClass(/tw-active/);
});

test('holding cmd+r peeks only while held, and the resolved hold does not also toggle mode', async ({ page }) => {
	await page.keyboard.down('Control');
	await page.keyboard.press('r'); // enter overlay mode first
	await page.keyboard.up('Control');
	await expect(page.locator('.perf-root')).toHaveClass(/tw-active/);

	await page.keyboard.down('Control');
	await page.keyboard.down('r');
	// The gesture's own hold threshold is real time (350ms) - wait past it.
	await page.waitForTimeout(450);
	await expect.poll(() => techState(page).then((s) => s.peeking)).toBe(true);

	await page.keyboard.up('r');
	await page.keyboard.up('Control');
	await expect.poll(() => techState(page).then((s) => s.peeking)).toBe(false);
	await expect
		.poll(() => techState(page).then((s) => s.active), { message: 'a resolved hold must not also toggle mode' })
		.toBe(true);
});

test('cmd+e toggles the eq overlay and ignores real OS key-repeat', async ({ page }) => {
	await page.keyboard.down('Control');
	await page.keyboard.press('r');
	await page.keyboard.up('Control');

	await page.keyboard.down('Control');
	await page.keyboard.press('e');
	await page.keyboard.up('Control');
	await expect.poll(() => techState(page).then((s) => s.eq_raised)).toBe(true);

	// A real KeyboardEvent with repeat:true, dispatched through the actual
	// window listener - this is what an OS auto-repeat delivers, not a
	// substitute for it. Playwright's high-level keyboard API cannot force
	// the browser's own repeat timing deterministically, so the flag is set
	// directly on a real event through the real DOM dispatch path.
	await page.evaluate(() => {
		window.dispatchEvent(
			new KeyboardEvent('keydown', { key: 'e', ctrlKey: true, repeat: true, cancelable: true })
		);
	});
	await expect
		.poll(() => techState(page).then((s) => s.eq_raised), { message: 'repeat must not re-toggle back off' })
		.toBe(true);

	await page.keyboard.down('Control');
	await page.keyboard.press('e');
	await page.keyboard.up('Control');
	await expect.poll(() => techState(page).then((s) => s.eq_raised)).toBe(false);
});

test('hotkeys are suppressed while the settings overlay is open', async ({ page }) => {
	await page.keyboard.down('Control');
	await page.keyboard.press(',');
	await page.keyboard.up('Control');
	await expect(page.locator('[aria-label="Settings"]')).toBeVisible();

	await page.keyboard.down('Control');
	await page.keyboard.press('r');
	await page.keyboard.up('Control');
	await expect
		.poll(() => techState(page).then((s) => s.active), {
			message: 'cmd+r must not toggle while Settings is open'
		})
		.toBe(false);

	await page.keyboard.press('Escape');
	await expect(page.locator('[aria-label="Settings"]')).toBeHidden();
});

test('hotkeys are suppressed while the event target is a text input', async ({ page }) => {
	const search = page.locator('.rb-search input');
	await search.click();
	await page.keyboard.down('Control');
	await page.keyboard.press('e');
	await page.keyboard.up('Control');
	await expect
		.poll(() => techState(page).then((s) => s.eq_raised), {
			message: 'typing in a field must not raise the eq overlay'
		})
		.toBe(false);
});

test('losing window focus resolves an in-flight hold instead of sticking it down', async ({ page }) => {
	await page.keyboard.down('Control');
	await page.keyboard.press('r');
	await page.keyboard.up('Control');

	await page.keyboard.down('Control');
	await page.keyboard.down('r');
	await page.waitForTimeout(450); // cross the real hold threshold first
	await expect
		.poll(() => techState(page).then((s) => s.peeking), { message: 'the hold must have resolved before blur' })
		.toBe(true);

	await page.evaluate(() => window.dispatchEvent(new Event('blur')));
	await expect
		.poll(() => techState(page).then((s) => s.peeking), {
			message: 'blur must resolve the gesture, not leave it dangling'
		})
		.toBe(false);
	await expect
		.poll(() => techState(page).then((s) => s.active), { message: 'a resolved hold must not also toggle mode' })
		.toBe(true);

	// The dangling native keyup for 'r' never arrives once focus is lost
	// (the OS delivered it to whatever window now has focus), so the real
	// key is still physically down from the browser's point of view -
	// release it before the next assertion or the following press starts
	// from an already-down state.
	await page.keyboard.up('r');

	// Proof it is not stuck "down": a fresh press now toggles cleanly.
	await page.keyboard.down('Control');
	await page.keyboard.press('r');
	await page.keyboard.up('Control');
	await expect
		.poll(() => techState(page).then((s) => s.active), {
			message: 'toggled off - the post-blur press/tap landed'
		})
		.toBe(false);
});

test('pointermove near an edge only tracks hover while tech mode is active', async ({ page }) => {
	await page.mouse.move(5, 400);
	await expect
		.poll(() => techState(page).then((s) => s.hovered_edges), {
			message: 'tech mode is off - hover tracking must be inert'
		})
		.toEqual([]);

	await page.keyboard.down('Control');
	await page.keyboard.press('r');
	await page.keyboard.up('Control');

	await page.mouse.move(6, 400);
	await expect.poll(() => techState(page).then((s) => s.hovered_edges)).toEqual(['left']);

	// Moving off the 40px trigger strip but still over the revealed left
	// column (which the earlier hover opened) must NOT drop the reveal -
	// this is the sticky-region fix: proximity alone opens a reveal, but
	// leaving the strip while still over what it opened must not close it.
	await page.mouse.move(300, 400);
	await expect
		.poll(() => techState(page).then((s) => s.hovered_edges), {
			message: 'moving into the revealed left column must not close it before it can be used'
		})
		.toEqual(['left']);

	// Moving into the mixer, clear of both deck columns, must close it.
	await page.mouse.move(640, 400);
	await expect.poll(() => techState(page).then((s) => s.hovered_edges)).toEqual([]);
});

test('left/right edge reveal is gated per deck by real load state, not just hover proximity', async ({
	page,
	request
}) => {
	// deck 1 sits on the left edge, deck 2 on the right (DECK_COLUMN_EDGE in
	// +page.svelte). Load a REAL track onto deck 1 only, through the same
	// dispatcher a real user/agent action drives, then confirm the left
	// column reveals on hover while the still-empty right column does not -
	// isDeckSlotVisible's `isDeckLoaded` predicate against production state,
	// not a fabricated fixture.
	const stableId = await realTrackStableId(request);
	const root = page.locator('.perf-root');

	await page.evaluate(
		(id) => window.musicDjToolsPerformance!.dispatch({ type: 'load', deck: 1, stable_id: id }),
		stableId
	); // page.evaluate awaits the returned promise before resolving
	await expect
		.poll(() => page.evaluate(() => window.musicDjToolsPerformance!.query().decks[1].stable_id), {
			message: 'deck 1 must show the real load before tech mode is exercised'
		})
		.not.toBeNull();

	await page.keyboard.down('Control');
	await page.keyboard.press('r');
	await page.keyboard.up('Control');

	await page.mouse.move(6, 400);
	await expect(root).toHaveClass(/tw-deck1-visible/);

	await page.mouse.move(1994, 400);
	await expect(root, 'deck 2 is not loaded - proximity alone must not reveal it').not.toHaveClass(
		/tw-deck2-visible/
	);
});

async function backdropOpacity(page: Page): Promise<string> {
	return page.evaluate(() => {
		const root = document.querySelector('.perf-root');
		if (root === null) throw new Error('.perf-root is not mounted');
		return getComputedStyle(root, '::before').backgroundColor;
	});
}

test('the homeless-group backdrop shows only for the Opt reveal, never for a cmd+r peek', async ({
	page
}) => {
	await page.keyboard.down('Control');
	await page.keyboard.press('r');
	await page.keyboard.up('Control');

	// Opt held: the backdrop must paint, dimming what is still hidden behind
	// the revealed topbar/mixer.
	await page.keyboard.down('Alt');
	await expect
		.poll(() => backdropOpacity(page), { message: 'Opt-held reveal must paint the dimming backdrop' })
		.toBe('rgba(0, 0, 0, 0.55)');
	await page.keyboard.up('Alt');
	await expect.poll(() => backdropOpacity(page)).toBe('rgba(0, 0, 0, 0)');

	// cmd+r held (peek): every region is already fully visible, so the
	// backdrop must NOT paint - it would dim the content peek just restored.
	await page.keyboard.down('Control');
	await page.keyboard.down('r');
	await page.waitForTimeout(450);
	await expect
		.poll(() => page.evaluate(() => window.musicDjToolsPerformance!.query().technically_working.peeking))
		.toBe(true);
	await expect
		.poll(() => backdropOpacity(page), {
			message: 'a cmd+r peek must not dim the UI it just brought back into view'
		})
		.toBe('rgba(0, 0, 0, 0)');
	await page.keyboard.up('r');
	await page.keyboard.up('Control');
});

// LIBUX-29: in a browser tab Cmd+R is the browser's reload chord. Every test
// in this file runs in a tab (no desktop shell global), which is the case
// under test here.
test('cmd+r in a browser tab is left to the browser and does not enter overlay mode', async ({
	page
}) => {
	const root = page.locator('.perf-root');
	await page.evaluate(() => {
		const probe = window as unknown as { __reloadChord: { defaultPrevented: boolean } | null };
		probe.__reloadChord = null;
		// Registered on document, so it runs after the window listeners under
		// test, and it reads the flag after the dispatch has finished.
		document.addEventListener('keydown', (event) => {
			if (!event.metaKey || event.key.toLowerCase() !== 'r') return;
			setTimeout(() => {
				probe.__reloadChord = { defaultPrevented: event.defaultPrevented };
			});
		});
	});

	await page.keyboard.press('Meta+r');
	await page.waitForFunction(
		() => (window as unknown as { __reloadChord: unknown }).__reloadChord !== null
	);
	const chord = await page.evaluate(
		() => (window as unknown as { __reloadChord: { defaultPrevented: boolean } }).__reloadChord
	);
	expect(chord.defaultPrevented, 'the page must not swallow the reload chord').toBe(false);
	// A reload may or may not follow (headless Chromium has no accelerator for
	// it); either way the surface that is on screen is not in overlay mode.
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await expect(root).not.toHaveClass(/tw-active/);
	expect((await techState(page)).active).toBe(false);
});

// LIBUX-30: a hidden region takes its separator rules with it.
test('overlay mode leaves no deck-column or wave-stack rule painted, and each returns with its edge', async ({
	page
}) => {
	const root = page.locator('.perf-root');
	const TRANSPARENT = 'rgba(0, 0, 0, 0)';
	const rules = () =>
		page.evaluate(() => {
			const color = (selector: string, side: 'Right' | 'Left' | 'Bottom') => {
				const element = document.querySelector(selector);
				if (element === null) throw new Error(`${selector} not found`);
				const style = getComputedStyle(element);
				if (Number.parseFloat(style[`border${side}Width`]) <= 0) {
					throw new Error(`${selector} has no ${side} border to measure`);
				}
				return style[`border${side}Color`];
			};
			return {
				left: color('.perf-root .deck-col:first-child', 'Right'),
				right: color('.perf-root .deck-col:last-child', 'Left'),
				top: color('.perf-root .rb-wavestack', 'Bottom')
			};
		});

	// Positive control: outside overlay mode all three rules are painted, so
	// "transparent" below is a change and not how they always read.
	const before = await rules();
	for (const [edge, color] of Object.entries(before)) {
		expect(color, `${edge} rule outside overlay mode`).not.toBe(TRANSPARENT);
	}

	await page.keyboard.down('Control');
	await page.keyboard.press('r');
	await page.keyboard.up('Control');
	await expect(root).toHaveClass(/tw-active/);
	await page.mouse.move(640, 400);
	await expect(root).not.toHaveClass(/tw-top-visible/);
	await expect.poll(rules).toEqual({ left: TRANSPARENT, right: TRANSPARENT, top: TRANSPARENT });

	// Opposite direction: revealing an edge brings back that edge's rule and
	// only that one, so the fix cannot be "never paint them again".
	await page.mouse.move(1000, 5);
	await expect(root).toHaveClass(/tw-top-visible/);
	await expect
		.poll(rules)
		.toEqual({ left: TRANSPARENT, right: TRANSPARENT, top: before.top });
});
