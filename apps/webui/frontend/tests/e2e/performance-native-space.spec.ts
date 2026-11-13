/**
 * Issue #12281 (A11Y-01): once the real /performance page has installed its
 * global capture-phase keydown listener, Space on a focused native control
 * belongs to that control, and Space on inert page chrome belongs to deck
 * transport. tests/unit/performance-native-space.test.mjs drives the same
 * installed listener with stand-in targets; only a browser proves the native
 * activation itself, so this file presses a REAL Space at REAL focused
 * elements. Uses the performance fixture library
 * (playwright.performance.config.ts) and keeps master muted throughout.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const SETTLE_MS = 750;

type SpaceProbe = { clicks: number; keydowns: { tag: string; defaultPrevented: boolean }[] };

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

async function deckOne(page: Page): Promise<{ playing: boolean; transport_pending: boolean }> {
	return page.evaluate(() => {
		const deck = window.musicDjToolsPerformance!.query().decks[1];
		return { playing: deck.playing, transport_pending: deck.transport_pending };
	});
}

/**
 * Boots /performance, mutes master, loads deck 1 paused, and mounts one real
 * native button and one real native checkbox. A window BUBBLE listener, added
 * after the app's capture listener, records whether each Space keydown
 * reached the page default-prevented.
 */
async function openWithPausedDeckAndProbes(page: Page, request: APIRequestContext): Promise<void> {
	const stableId = await firstOnDiskStableId(request);
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 60_000
	});
	await page.evaluate(async (id) => {
		const perf = window.musicDjToolsPerformance!;
		await perf.dispatch({ type: 'master_volume', value: 0 });
		await perf.dispatch({ type: 'master_mute', muted: true, persist: false });
		await perf.dispatch({ type: 'load', deck: 1, stable_id: id });
	}, stableId);
	await page.waitForFunction(
		(id) => window.musicDjToolsPerformance?.query().decks[1].stable_id === id,
		stableId,
		{ timeout: 90_000 }
	);
	await page.evaluate(() =>
		window.musicDjToolsPerformance!.dispatch({ type: 'play', deck: 1, playing: false })
	);
	await expect.poll(() => deckOne(page)).toEqual({ playing: false, transport_pending: false });

	await page.evaluate(() => {
		const probe: SpaceProbe = { clicks: 0, keydowns: [] };
		(window as unknown as { __nativeSpaceProbe: SpaceProbe }).__nativeSpaceProbe = probe;
		const host = document.createElement('div');
		host.style.cssText = 'position:fixed;left:8px;bottom:8px;z-index:2147483647';
		const button = document.createElement('button');
		button.type = 'button';
		button.textContent = 'Native space probe';
		button.dataset.testid = 'native-space-button';
		button.addEventListener('click', () => {
			probe.clicks += 1;
		});
		const checkbox = document.createElement('input');
		checkbox.type = 'checkbox';
		checkbox.dataset.testid = 'native-space-checkbox';
		host.append(button, checkbox);
		document.body.append(host);
		window.addEventListener('keydown', (event) => {
			if (event.code !== 'Space') return;
			const target = event.target instanceof Element ? event.target.tagName : 'NONE';
			probe.keydowns.push({ tag: target, defaultPrevented: event.defaultPrevented });
		});
	});
}

async function readProbe(page: Page): Promise<SpaceProbe> {
	return page.evaluate(
		() => (window as unknown as { __nativeSpaceProbe: SpaceProbe }).__nativeSpaceProbe
	);
}

/** Every distinct document and `.table-wrap` scrollTop seen across `ms` of frames while `during` runs. */
async function sampleScroll(
	page: Page,
	ms: number,
	during: () => Promise<void>
): Promise<{ wrap: number[]; doc: number[] }> {
	await page.evaluate((windowMs) => {
		const wrapEl = document.querySelector('.table-wrap');
		const wrap = new Set<number>();
		const doc = new Set<number>();
		const end = performance.now() + windowMs;
		(window as unknown as { __scrollSample: Promise<unknown> }).__scrollSample = new Promise(
			(resolve) => {
				const tick = () => {
					wrap.add(wrapEl instanceof HTMLElement ? wrapEl.scrollTop : 0);
					doc.add(document.scrollingElement?.scrollTop ?? 0);
					if (performance.now() >= end) resolve({ wrap: [...wrap], doc: [...doc] });
					else requestAnimationFrame(tick);
				};
				tick();
			}
		);
	}, ms);
	await during();
	return page.evaluate(
		() =>
			(window as unknown as { __scrollSample: Promise<{ wrap: number[]; doc: number[] }> })
				.__scrollSample
	);
}

test.describe('native Space activation after the /performance listener installs (#12281)', () => {
	test.beforeEach(async ({ page, request }) => {
		await openWithPausedDeckAndProbes(page, request);
	});

	test('[if] a focused button receives Space [then] it activates itself and deck transport stays paused, [else stop].', async ({
		page
	}) => {
		const button = page.getByTestId('native-space-button');
		await button.focus();
		await expect(button).toBeFocused();

		await page.keyboard.press('Space');

		await expect.poll(async () => (await readProbe(page)).clicks).toBe(1);
		await page.waitForTimeout(SETTLE_MS);
		const probe = await readProbe(page);
		expect(probe.keydowns).toEqual([{ tag: 'BUTTON', defaultPrevented: false }]);
		expect(await deckOne(page)).toEqual({ playing: false, transport_pending: false });
		await expect(button).toBeFocused();
	});

	test('[if] a focused checkbox receives Space [then] only its own state changes and transport stays paused, [else stop].', async ({
		page
	}) => {
		const checkbox = page.getByTestId('native-space-checkbox');
		await checkbox.focus();
		await expect(checkbox).toBeFocused();
		await expect(checkbox).not.toBeChecked();

		await page.keyboard.press('Space');

		await expect(checkbox).toBeChecked();
		await page.waitForTimeout(SETTLE_MS);
		const probe = await readProbe(page);
		expect(probe.clicks, 'the neighbouring button must not activate').toBe(0);
		expect(probe.keydowns).toEqual([{ tag: 'INPUT', defaultPrevented: false }]);
		expect(await deckOne(page)).toEqual({ playing: false, transport_pending: false });
	});

	test('[if] inert performance chrome receives Space [then] transport toggles and the page does not scroll, [else stop].', async ({
		page
	}) => {
		await page.evaluate(() => {
			if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
		});
		expect(await page.evaluate(() => document.activeElement === document.body)).toBe(true);
		const docBefore = await page.evaluate(() => document.scrollingElement?.scrollTop ?? 0);
		const wrapBefore = await page.evaluate(() => {
			const wrapEl = document.querySelector('.table-wrap');
			return wrapEl instanceof HTMLElement ? wrapEl.scrollTop : 0;
		});

		const sampled = await sampleScroll(page, SETTLE_MS, async () => {
			await page.keyboard.press('Space');
		});

		expect(sampled.doc, 'Space must never scroll the document').toEqual([docBefore]);
		expect(sampled.wrap, 'Space must never scroll the library table').toEqual([wrapBefore]);
		const probe = await readProbe(page);
		expect(probe.keydowns).toEqual([{ tag: 'BODY', defaultPrevented: true }]);
		expect(probe.clicks).toBe(0);
		await expect.poll(async () => (await deckOne(page)).playing, { timeout: 30_000 }).toBe(true);
		await page.evaluate(() =>
			window.musicDjToolsPerformance!.dispatch({ type: 'play', deck: 1, playing: false })
		);
	});
});
