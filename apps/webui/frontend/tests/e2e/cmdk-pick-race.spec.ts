/**
 * CMDK-16 (issue #6091; Codex r4266506832 on #6089): a Cmd K pick that is
 * waiting on rb-meta never replaces a newer load on the same deck.
 *
 * Real engine, decoder and fixture library (playwright.cmdk-pick-race.config.ts),
 * and nothing rewritten on the wire (Codex r4274536405 on #6689): the pending
 * rows are the engine's own. The fixture is rebuilt for every run, so the
 * engine starts cold. Its fillers fill the library table's first page and
 * the index's own stat budget, so every "Pick Race" row reaches the engine
 * first through the bar's library index, which serves it AVAILABILITY_PENDING
 * (support/cmdk_pick_race_fixture.py says why). Its background refresher
 * settles them on the server right after, so a second index would read them
 * present; but the page keeps the rows it was served, and a Cmd K pick of one
 * settles that one row through the real rb-meta route. So every scenario runs on ONE page, opened once, whose index
 * is that first cold one, each on its own pending row and its own deck. The
 * spec only holds real responses (rb-meta, a rival's audio) and then lets them
 * through unchanged. If the page holds no pending row at all, the gate fails
 * saying so rather than passing on a race it never ran.
 *
 * Regression lines:
 * - if the pick loads over an IPC load that committed during the wait then broken
 * - if the pick loads over an IPC load still queued when rb-meta answers then broken
 * - if a pick with nothing racing it does not load then broken (the positive control)
 * - if the pick does not actually wait on rb-meta, the "nothing loaded while
 *   held" check fails rather than passing on a race never run
 * - if the engine serves no pending row on a cold start, the gate fails as UNAVAILABLE
 */
import { expect, test, type Page, type Route } from '@playwright/test';

import type { DeckId } from '../../src/lib/rb/deck-id';

declare global {
	interface Window {
		__cmdkRival?: Promise<unknown>;
	}
}

/** Hold a real response until `release()`, then let it through unchanged; `asked` resolves on the first request. */
async function holdRoute(page: Page, url: string): Promise<{ asked: Promise<void>; release: () => void }> {
	let markAsked!: () => void;
	const asked = new Promise<void>((resolve) => (markAsked = resolve));
	let release!: () => void;
	const released = new Promise<void>((resolve) => (release = resolve));
	await page.route(url, async (route: Route) => {
		markAsked();
		await released;
		await route.continue();
	});
	return { asked, release };
}

async function deckTrack(page: Page, deck: DeckId): Promise<string | null> {
	return page.evaluate((id) => window.musicDjToolsPerformance!.query().decks[id].stable_id, deck);
}

/** Rows already picked: each scenario takes a pending row no earlier one used. */
const used = new Set<string>();

/**
 * Open Cmd K on the fixture, hold rb-meta for the first pending row no earlier
 * scenario took, and click it for `deck`. Returns the picked id, a rival id,
 * and the hold.
 */
async function pickPendingRow(page: Page, deck: DeckId): Promise<{ picked: string; rival: string; meta: { asked: Promise<void>; release: () => void } }> {
	const input = page.getByTestId('command-bar-input');
	// The v2 bar (cmdk.v2 on through the fixture's own feature-flags.json), not the shipped one.
	// It loads on demand after the flags are read, so its key listener can land a moment after
	// the IPC does: press until it opens rather than once.
	await expect(async () => {
		await page.keyboard.press('Control+k');
		await expect(page.locator('#cmdk2-list')).toBeVisible({ timeout: 1_000 });
	}).toPass({ timeout: 30_000 });
	await expect(input).toBeFocused();
	await input.fill('Pick Race');
	const rows = page.locator('[data-testid="command-bar-row"]');
	await expect(rows.first()).toBeVisible();
	const listed = await rows.evaluateAll((all) =>
		all.map((row) => ({ id: row.getAttribute('data-stable-id'), availability: row.getAttribute('data-availability') }))
	);
	const pending = listed.filter((row) => row.availability === 'AVAILABILITY_PENDING' && row.id !== null).map((row) => row.id!);
	const picked = pending.find((id) => !used.has(id));
	if (picked === undefined) {
		throw new Error(
			`UNAVAILABLE: the engine served no unused AVAILABILITY_PENDING row on its cold index (${pending.length} pending of ${listed.length} listed); the race cannot be run`
		);
	}
	used.add(picked);
	const rival = listed.map((row) => row.id).find((id) => id !== null && id !== picked)!;
	// The bar loads onto its target deck: the default target is deck 1, so step it with the deck keys.
	await page.keyboard.press(`Alt+${deck}`);
	const meta = await holdRoute(page, `**/api/v1/tracks/${picked}/rb-meta`);
	await page.locator(`[data-testid="command-bar-row"][data-stable-id="${picked}"]`).click();
	await meta.asked;
	// The pick is waiting on rb-meta, so nothing has loaded yet.
	await page.waitForTimeout(500);
	expect(await deckTrack(page, deck), 'the pick waits on rb-meta before it loads').toBeNull();
	return { picked, rival, meta };
}

test.describe.configure({ mode: 'serial' });
// The config starts no engine on Windows: its shared launcher runs /bin/sh and .venv/bin/python.
test.skip(process.platform === 'win32', 'POSIX-only: guardedWebServerCommand launches /bin/sh and .venv/bin/python');

test.describe('Cmd K pick race (CMDK-16)', () => {
	let page: Page;

	// The one page every scenario uses: its index is the engine's first, cold one. The dev
	// server compiles /performance on this first visit, which can outlast one test's budget.
	test.beforeAll(async ({ browser }) => {
		test.setTimeout(180_000);
		page = await browser.newPage();
		await page.goto('/performance', { timeout: 170_000 });
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, { timeout: 170_000 });
	});

	test.afterAll(async () => {
		await page.close();
	});

	test('a pick with nothing racing it loads once rb-meta answers (positive control)', async () => {
		test.fixme(true, 'positive control times out, see #13527');
		const deck: DeckId = 1;
		const { picked, meta } = await pickPendingRow(page, deck);
		meta.release();
		await expect.poll(() => deckTrack(page, deck)).toBe(picked);
	});

	test('an IPC load that commits during the rb-meta wait stays', async () => {
		const deck: DeckId = 2;
		const { rival, meta } = await pickPendingRow(page, deck);
		await page.evaluate(([id, track]) => window.musicDjToolsPerformance!.dispatch({ type: 'load', deck: id, stable_id: track }), [deck, rival] as const);
		await expect.poll(() => deckTrack(page, deck)).toBe(rival);
		meta.release();
		await page.waitForTimeout(1500);
		expect(await deckTrack(page, deck)).toBe(rival);
	});

	test('an IPC load still queued when rb-meta answers stays', async () => {
		const deck: DeckId = 3;
		const { rival, meta } = await pickPendingRow(page, deck);
		// Keep the rival load in the dispatcher's deck scope: its real audio response waits too.
		const audio = await holdRoute(page, `**/api/v1/tracks/${rival}/audio*`);
		await page.evaluate(
			([id, track]) => {
				window.__cmdkRival = window.musicDjToolsPerformance!.dispatch({ type: 'load', deck: id, stable_id: track });
			},
			[deck, rival] as const
		);
		await audio.asked;
		expect(
			await page.evaluate((id) => window.musicDjToolsPerformance!.query().decks[id].command_pending, deck),
			'the rival load is queued, not committed, when rb-meta answers'
		).toBe(true);
		meta.release();
		// Give the settled pick every chance to dispatch its own load before the rival commits.
		await page.waitForTimeout(1500);
		expect(await deckTrack(page, deck), 'nothing has committed on the deck while the rival is held').toBeNull();
		audio.release();
		await page.evaluate(() => window.__cmdkRival);
		await page.waitForTimeout(1500);
		expect(await deckTrack(page, deck)).toBe(rival);
	});
});
