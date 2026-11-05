import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { fixtureManifestExists, readFixtureManifest } from './support/fixture-manifest';

/**
 * DECKUX-8360 (the maintainer, Tue 3 Nov 2026, deck 2 screenshot): a mirrored deck is a
 * TRUE horizontal mirror, so the controls he reaches for most (BEAT SYNC,
 * MASTER, KEY SYNC, jog, CUE/play) sit beside the screen centre and the
 * artwork sits on the outer edge. Real page, real fixture tracks, real layout
 * boxes: each check is a left-to-right order of on-screen centres.
 *
 * DECK_MIRROR_SHOTS_DIR set: also writes decks 1+2 side-by-side screenshots
 * at 1920 and 3440 widths (the PR's before/after evidence).
 * -Claude
 */

const MANIFEST_PATH = fileURLToPath(
	new URL('./fixtures/performance-playwright-data/fixture-manifest.json', import.meta.url)
);
const FIXTURE_MODE_READY =
	process.env.PERFORMANCE_E2E_FIXTURE !== '0' && fixtureManifestExists(MANIFEST_PATH);
const SHOTS_DIR = process.env.DECK_MIRROR_SHOTS_DIR ?? '';
const SHOT_LABEL = process.env.DECK_MIRROR_SHOTS_LABEL ?? 'after';
const VIEWPORTS = [
	{ width: 1920, height: 1080 },
	{ width: 3440, height: 1440 }
] as const;

// The suite can attach to a caller's backend, so each test sets the right-deck
// mirror pref on disk itself (the boot GET hydrates it) and puts back what it found.
async function readRightMirror(request: APIRequestContext): Promise<boolean> {
	const res = await request.get('/api/v1/ui-prefs');
	expect(res.ok()).toBe(true);
	const value = (await res.json()).deck_right_mirror;
	expect(typeof value).toBe('boolean');
	return value;
}

async function setRightMirror(request: APIRequestContext, on: boolean): Promise<void> {
	const res = await request.put('/api/v1/ui-prefs', { data: { deck_right_mirror: on } });
	expect(res.ok()).toBe(true);
	expect((await res.json()).deck_right_mirror).toBe(on);
	expect(await readRightMirror(request), 'deck_right_mirror round-trips through disk').toBe(on);
}

async function dispatch(page: Page, command: Record<string, unknown>): Promise<void> {
	await page.evaluate((command) => {
		if (window.musicDjToolsPerformance === undefined) throw new Error('performance IPC unavailable');
		return window.musicDjToolsPerformance.dispatch(command as never);
	}, command);
}

async function openWithTwoDecks(page: Page, viewport: { width: number; height: number }): Promise<void> {
	await page.setViewportSize(viewport);
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const { tracks } = readFixtureManifest(MANIFEST_PATH);
	await dispatch(page, { type: 'master_volume', value: 0 });
	await dispatch(page, { type: 'load', deck: 1, stable_id: tracks[0]!.stable_id });
	await dispatch(page, { type: 'load', deck: 2, stable_id: tracks[1]!.stable_id });
	for (const id of [1, 2]) {
		await expect(page.getByTestId(`unload-deck-${id}`)).toBeVisible();
	}
}

/** The named parts of one deck, by testid / ARIA label only. */
function deckParts(page: Page, id: 1 | 2): Record<string, Locator> {
	const deck = page.locator(`.rb-deck[data-deck="${id}"]`);
	return {
		art: page.getByTestId(`unload-deck-${id}`),
		title: deck.locator('.deck-header .title'),
		bpm: page.getByTestId(`tempo-readout-deck-${id}`),
		keySync: page.getByTestId(`key-sync-deck-${id}`),
		beatSync: page.getByTestId(`beat-sync-deck-${id}`),
		master: page.getByTestId(`master-deck-${id}`),
		hotCueMenu: page.getByTestId(`hot-cue-menu-deck-${id}`),
		cueGrid: deck.getByRole('group', { name: `hot cues deck ${id}` }),
		cue: page.getByTestId(`cue-deck-${id}`),
		dial: deck.getByRole('group', { name: `jog controls deck ${id}` }).locator('.dial-wrap'),
		jogMods: deck.getByRole('group', { name: `jog controls deck ${id}` }).locator('.side-buttons'),
		pitch: page.getByTestId(`pitch-fader-deck-${id}`)
	};
}

async function centreX(locator: Locator, name: string): Promise<number> {
	const box = await locator.first().boundingBox();
	expect(box, `${name} has a layout box`).not.toBeNull();
	return box!.x + box!.width / 2;
}

/** Assert the named parts read left to right in exactly this order. */
async function expectLeftToRight(parts: Record<string, Locator>, order: string[], label: string): Promise<void> {
	const xs: number[] = [];
	for (const name of order) xs.push(await centreX(parts[name]!, `${label} ${name}`));
	for (let i = 1; i < order.length; i++) {
		expect(xs[i]!, `${label}: ${order[i - 1]} (x=${xs[i - 1]}) must sit left of ${order[i]} (x=${xs[i]})`).toBeGreaterThan(xs[i - 1]!);
	}
}

const HEADER_LTR = ['art', 'title', 'bpm', 'keySync', 'beatSync'];
const BODY_LTR = ['hotCueMenu', 'cueGrid', 'cue', 'dial', 'jogMods', 'pitch'];

test('DECKUX-8360: mirrored deck 2 is a true mirror of unmirrored deck 1', async ({ page, request }) => {
	test.skip(!FIXTURE_MODE_READY, 'needs the generated fixture library (fixture mode)');
	const original = await readRightMirror(request);
	try {
		await setRightMirror(request, true);
		for (const viewport of VIEWPORTS) {
			await openWithTwoDecks(page, viewport);
			await expect(page.locator('.rb-deck[data-deck="2"] .deck-header')).toHaveAttribute('data-mirrored', 'true');
			await expect(page.locator('.rb-deck[data-deck="1"] .deck-header')).toHaveAttribute('data-mirrored', 'false');
			const one = deckParts(page, 1);
			const two = deckParts(page, 2);
			const at = `${viewport.width}px`;
			await expectLeftToRight(one, HEADER_LTR, `${at} deck 1 header`);
			await expectLeftToRight(one, BODY_LTR, `${at} deck 1 body`);
			await expectLeftToRight(two, [...HEADER_LTR].reverse(), `${at} deck 2 header`);
			await expectLeftToRight(two, [...BODY_LTR].reverse(), `${at} deck 2 body`);
			// MASTER stays stacked under BEAT SYNC, on the inner edge with it.
			expect(Math.abs((await centreX(two.master, 'master')) - (await centreX(two.beatSync, 'beat sync')))).toBeLessThan(2);
			// Text still reads left to right: no flipped glyphs.
			expect(await two.title.evaluate((el) => getComputedStyle(el).direction)).toBe('ltr');
			expect(await two.title.evaluate((el) => getComputedStyle(el).transform)).toBe('none');
		}
	} finally {
		await setRightMirror(request, original);
	}
});

test('DECKUX-8360 evidence: decks 1+2 side-by-side screenshots', async ({ page, request }) => {
	test.skip(!FIXTURE_MODE_READY, 'needs the generated fixture library (fixture mode)');
	test.skip(SHOTS_DIR === '', 'DECK_MIRROR_SHOTS_DIR unset');
	mkdirSync(SHOTS_DIR, { recursive: true });
	const original = await readRightMirror(request);
	try {
		await setRightMirror(request, true);
		for (const viewport of VIEWPORTS) {
			await openWithTwoDecks(page, viewport);
			// The pref really reached the page (true on the pre-change head as well).
			await expect(page.locator('.rb-deck[data-deck="2"] .main-row')).toHaveClass(/mirror-main-row/);
			await expect(page.locator('.rb-deck[data-deck="1"] .main-row')).not.toHaveClass(/mirror-main-row/);
			await page.mouse.move(1, 1);
			await page.waitForTimeout(500);
			const a = (await page.locator('.rb-deck[data-deck="1"]').boundingBox())!;
			const b = (await page.locator('.rb-deck[data-deck="2"]').boundingBox())!;
			const x = Math.min(a.x, b.x);
			const y = Math.min(a.y, b.y);
			await page.screenshot({
				path: join(SHOTS_DIR, `decks-1-2-${viewport.width}-${SHOT_LABEL}.png`),
				clip: { x, y, width: Math.max(a.x + a.width, b.x + b.width) - x, height: Math.max(a.y + a.height, b.y + b.height) - y }
			});
		}
	} finally {
		await setRightMirror(request, original);
	}
});
