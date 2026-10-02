/**
 * DECKUX-21 / LIBUX-21: Chromium drag-to-deck load and Space must not scroll
 * the library. Uses the performance fixture library (playwright.performance.config.ts).
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const TRACK_ROW = '[data-testid="track-row"]';
const TRACK_STABLE_MIME = 'application/x-mdt-stable-id';

/** Optional row probes; benign 404s on other tracks must not mask failures on the dragged row. */
const OPTIONAL_ROW_PROBE_PATTERNS: readonly RegExp[] = [
	/\/api\/v1\/tracks\/[^/]+\/artwork(\?|$)/,
	/\/api\/v1\/tracks\/[^/]+\/stems(\?|$)/
];

function isIgnorableOptionalRowProbe(url: string, draggedStableId: string): boolean {
	if (!OPTIONAL_ROW_PROBE_PATTERNS.some((pattern) => pattern.test(url))) return false;
	const match = url.match(/\/api\/v1\/tracks\/([^/]+)\//);
	if (match?.[1] === draggedStableId) return false;
	return true;
}

/**
 * The words route answers 404 for a track with no karaoke words by design
 * (LYR-07: the deck strip then falls back to cached lines). A freshly loaded
 * fixture track has none, so that one 404 for the DRAGGED row is the product
 * naming an absence, not an error the drag or Space caused.
 */
function isDocumentedLyricsAbsence(url: string, status: number, draggedStableId: string): boolean {
	if (status !== 404) return false;
	const match = url.match(/\/api\/v1\/tracks\/([^/]+)\/lyrics\/words(\?|$)/);
	return match?.[1] === draggedStableId;
}

const SCROLL_SETTLE_MS = 750;

/**
 * Samples `.table-wrap` and the document scroller every animation frame for
 * `ms`, starting NOW, and returns every distinct value seen. A single read
 * right after a key press can run before the browser applies the default
 * Space scroll (Sol P2 on #4082), so a window of frames is what proves the
 * browser never scrolled.
 */
async function sampleScroll(
	page: Page,
	ms: number,
	during?: () => Promise<void>
): Promise<{ wrap: number[]; doc: number[] }> {
	// Start the sampler WITHOUT awaiting it (evaluate would wait for the whole
	// window and the action would only run after sampling ended), act, then
	// collect. The positive control below is what caught that ordering bug.
	await page.evaluate((windowMs) => {
		const wrapEl = document.querySelector('.table-wrap');
		if (!(wrapEl instanceof HTMLElement)) throw new Error('.table-wrap missing');
		const wrap = new Set<number>();
		const doc = new Set<number>();
		const end = performance.now() + windowMs;
		(window as unknown as { __scrollSample: Promise<unknown> }).__scrollSample = new Promise(
			(resolve) => {
				const tick = () => {
					wrap.add(wrapEl.scrollTop);
					doc.add(document.scrollingElement?.scrollTop ?? 0);
					if (performance.now() >= end) resolve({ wrap: [...wrap], doc: [...doc] });
					else requestAnimationFrame(tick);
				};
				tick();
			}
		);
	}, ms);
	if (during !== undefined) await during();
	return page.evaluate(
		() =>
			(window as unknown as { __scrollSample: Promise<{ wrap: number[]; doc: number[] }> })
				.__scrollSample
	);
}

/**
 * Asserts Space leaves every scroller where it was for a whole settle window,
 * after a POSITIVE CONTROL on the same probe: a real wheel scroll of the same
 * table inside the same kind of window must be seen, so "unchanged" cannot
 * pass because the table cannot scroll or the sampler cannot see it.
 */
async function expectSpaceDoesNotScroll(page: Page): Promise<void> {
	const tableWrap = page.locator('.table-wrap');
	const room = await tableWrap.evaluate((el) => ({
		top: el.scrollTop,
		max: el.scrollHeight - el.clientHeight
	}));
	expect(
		room.max - room.top,
		'.table-wrap must have room to scroll down, or Space could not scroll it anyway'
	).toBeGreaterThan(4);

	// Aim the wheel at the table's ON-SCREEN part: at 1280x800 in MORE its
	// box is partly clipped by `.list-panel`, so the box centre can sit under
	// other chrome and the wheel would scroll nothing.
	const aim = await tableWrap.evaluate((wrap) => {
		const box = wrap.getBoundingClientRect();
		let top = Math.max(box.top, 0);
		let bottom = Math.min(box.bottom, window.innerHeight);
		for (let el = wrap.parentElement; el !== null; el = el.parentElement) {
			if (getComputedStyle(el).overflow === 'visible') continue;
			const clip = el.getBoundingClientRect();
			top = Math.max(top, clip.top);
			bottom = Math.min(bottom, clip.bottom);
		}
		// Below the sticky thead, inside the visible rows.
		const thead = wrap.querySelector('thead')?.getBoundingClientRect().bottom ?? top;
		const y = (Math.max(top, thead) + bottom) / 2;
		const x = box.left + Math.min(box.width, wrap.clientWidth) / 2;
		const hit = document.elementFromPoint(x, y);
		return { x, y, hitsTable: hit !== null && wrap.contains(hit) };
	});
	expect(aim.hitsTable, 'the wheel control must aim at a visible point of .table-wrap').toBe(true);
	const restoreFocus = await page.evaluate(() => {
		const active = document.activeElement;
		if (active instanceof HTMLElement) active.setAttribute('data-scroll-probe-focus', '1');
		return active instanceof HTMLElement;
	});
	const control = await sampleScroll(page, SCROLL_SETTLE_MS, async () => {
		await page.mouse.move(aim.x, aim.y);
		await page.mouse.wheel(0, 120);
	});
	expect(
		control.wrap.length,
		`positive control: a real wheel scroll must be seen by the probe, saw ${control.wrap.join(',')}`
	).toBeGreaterThan(1);
	await tableWrap.evaluate((el, top) => {
		el.scrollTop = top;
	}, room.top);
	await expect.poll(async () => tableWrap.evaluate((el) => el.scrollTop)).toBe(room.top);
	if (restoreFocus) {
		await page.locator('[data-scroll-probe-focus="1"]').focus();
		await page.evaluate(() =>
			document.querySelector('[data-scroll-probe-focus]')?.removeAttribute('data-scroll-probe-focus')
		);
	}

	const docBefore = await page.evaluate(() => document.scrollingElement?.scrollTop ?? 0);
	const sampled = await sampleScroll(page, SCROLL_SETTLE_MS, async () => {
		await page.keyboard.press('Space');
	});
	expect(sampled.wrap, 'Space must never scroll .table-wrap during the settle window').toEqual([
		room.top
	]);
	expect(sampled.doc, 'Space must never scroll the document during the settle window').toEqual([
		docBefore
	]);
}

async function firstOnDiskStableId(request: APIRequestContext): Promise<string> {
	const response = await request.get(`${API_BASE}/api/v1/tracks?limit=50&available=true`);
	expect(response.ok(), 'track listing must succeed').toBeTruthy();
	const payload = (await response.json()) as {
		items: { stable_id: string; file_exists: boolean }[];
	};
	const track = payload.items.find(
		(item) => item.file_exists && typeof item.stable_id === 'string' && item.stable_id.length > 0
	);
	expect(track, 'library must include at least one on-disk available track').toBeDefined();
	return track!.stable_id;
}

/**
 * A REAL pointer drag: Playwright's mouse down / move / up on Chromium goes
 * through the browser's own hit-testing and native drag machinery, so the row
 * must really start a drag and the deck must really be the element under the
 * pointer at drop. Capture-phase listeners record that the dragstart on the
 * row and the drop on the deck were browser-generated (`isTrusted`), which a
 * synthetic `dispatchEvent` can never be. The WebKit path (custom MIME hidden
 * during dragover, in-app drag state carries acceptance) is pinned by
 * tests/unit/track-drag-drop.test.mjs.
 */
async function dragRowToDeck(
	page: Page,
	stableId: string,
	deckId: number
): Promise<{ trustedDragStart: boolean; trustedDrop: boolean; payloadOnDrop: string }> {
	await page.evaluate(
		({ sid, deck, mime }) => {
			const probe = { trustedDragStart: false, trustedDrop: false, payloadOnDrop: '' };
			(window as unknown as { __dragDeckProbe: typeof probe }).__dragDeckProbe = probe;
			document.addEventListener(
				'dragstart',
				(event) => {
					const row = (event.target as Element | null)?.closest?.('[data-testid="track-row"]');
					if (row?.getAttribute('data-stable-id') === sid && event.isTrusted) {
						probe.trustedDragStart = true;
					}
				},
				{ capture: true }
			);
			document.addEventListener(
				'drop',
				(event) => {
					const target = (event.target as Element | null)?.closest?.('section.rb-deck');
					if (target?.getAttribute('data-deck') === String(deck) && event.isTrusted) {
						probe.trustedDrop = true;
						probe.payloadOnDrop = event.dataTransfer?.getData(mime) ?? '';
					}
				},
				{ capture: true }
			);
		},
		{ sid: stableId, deck: deckId, mime: TRACK_STABLE_MIME }
	);
	const source = page.locator(`${TRACK_ROW}[data-stable-id="${stableId}"] .c-artist`);
	const target = page.locator(`section.rb-deck[data-deck="${deckId}"]`);
	await expect(source).toBeVisible();
	await expect(target).toBeVisible();
	await source.dragTo(target);
	return page.evaluate(
		() =>
			(
				window as unknown as {
					__dragDeckProbe: { trustedDragStart: boolean; trustedDrop: boolean; payloadOnDrop: string };
				}
			).__dragDeckProbe
	);
}

test('performance: drag library row onto deck loads track and Space toggles play without scrolling library', async ({
	page,
	request
}) => {
	test.setTimeout(120_000);
	const stableId = await firstOnDiskStableId(request);
	const pageErrors: string[] = [];
	const lyricsAbsenceUrls = new Set<string>();
	let recording = false;
	page.on('pageerror', (err) => {
		if (recording) pageErrors.push(err.message);
	});
	page.on('console', (msg) => {
		if (!recording || msg.type() !== 'error') return;
		const text = msg.text();
		if (
			text.startsWith('Failed to load resource:') &&
			(isIgnorableOptionalRowProbe(msg.location().url, stableId) ||
				lyricsAbsenceUrls.has(msg.location().url) ||
				isDocumentedLyricsAbsence(msg.location().url, 404, stableId))
		) {
			return;
		}
		pageErrors.push(text);
	});
	page.on('response', (response) => {
		if (!recording || response.status() < 400) return;
		if (isIgnorableOptionalRowProbe(response.url(), stableId)) return;
		if (isDocumentedLyricsAbsence(response.url(), response.status(), stableId)) {
			lyricsAbsenceUrls.add(response.url());
			return;
		}
		pageErrors.push(`http ${response.status()}: ${response.url()}`);
	});
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 60_000
	});

	const row = page.locator(`${TRACK_ROW}[data-stable-id="${stableId}"]`);
	await row.scrollIntoViewIfNeeded();
	await expect(row).toBeVisible({ timeout: 60_000 });
	// Record from the gesture on: boot-time probes of routes this backend
	// does not serve (entitlements, update check, rescue snapshots, USB) are
	// page-load behavior, not something the drag or Space surfaced.
	recording = true;
	await row.click();

	const gesture = await dragRowToDeck(page, stableId, 1);
	expect(gesture.trustedDragStart, 'a real pointer drag must start on the library row').toBe(true);
	expect(gesture.trustedDrop, 'the real drag must drop on deck 1').toBe(true);
	expect(gesture.payloadOnDrop).toBe(stableId);

	await page.waitForFunction(
		(id) => window.musicDjToolsPerformance?.query().decks[1].stable_id === id,
		stableId,
		{ timeout: 90_000 }
	);

	await expectSpaceDoesNotScroll(page);

	await page.waitForFunction(
		() => window.musicDjToolsPerformance?.query().decks[1].playing === true,
		undefined,
		{ timeout: 30_000 }
	);
	expect(pageErrors, 'drag and Space must not surface page or console errors').toEqual([]);
});

function assertAllDecksUnloadedAndStopped(
	decks: Record<number, { stable_id: string | null; playing: boolean }>
): void {
	for (const deck of Object.values(decks)) {
		expect(deck.stable_id, 'deck must be unloaded before Space no-op check').toBeNull();
		expect(deck.playing, 'deck must be stopped before Space no-op check').toBe(false);
	}
}

test('performance: Space on library with no loaded deck does not scroll the table', async ({
	page
}) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 60_000
	});
	const tableWrap = page.locator('.table-wrap');
	await expect(tableWrap).toBeVisible({ timeout: 60_000 });

	const trackRow = page.locator(`${TRACK_ROW}[tabindex="0"]`).first();
	await expect(trackRow).toBeVisible({ timeout: 60_000 });

	const beforeSpace = await page.evaluate(() => window.musicDjToolsPerformance!.query());
	assertAllDecksUnloadedAndStopped(beforeSpace.decks);

	await trackRow.focus();
	await expect(trackRow).toBeFocused();

	await expectSpaceDoesNotScroll(page);
	await expect(trackRow).toBeFocused();

	const afterSpace = await page.evaluate(() => window.musicDjToolsPerformance!.query());
	assertAllDecksUnloadedAndStopped(afterSpace.decks);
});
