/**
 * Issue #12276 (A11Y-01): a held Cmd/Ctrl grid-jump chord on /performance, in
 * a real browser with real key auto-repeat (Playwright sets `repeat: true` on
 * every `keyboard.down` of a key already down).
 *
 *   [if] Cmd or Ctrl plus a grid-jump key is held [then ⛔️] only the initial keydown dispatches grid_jump
 *   [if] a repeat keydown arrives [then ⛔️] browser Select All and bookmark actions do not run
 *   [if] focus is in a text field [then ⛔️] the grid-jump chord does not dispatch
 *
 * The `deck.snapped_jump` flag is off by default, so the flags response is
 * patched to declare it on. The count comes from the bus history
 * (`query().history`, ids `pc-<seq>`), and a window CAPTURE probe installed
 * before the app records each keydown's `defaultPrevented` once dispatch ends:
 * the app stops propagation at window capture, so a later listener never sees
 * the chord. A headless browser shows no bookmark dialog, so a prevented
 * default is the proof for Cmd/Ctrl+D; Cmd/Ctrl+A also checks the selection.
 *
 * The unit file grid-jump-keys-repeat.test.mjs drives the same listener in node.
 */
import { expect, test, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

const SETTLE_MS = 500;
const HELD_KEYDOWNS = 4;
const MODIFIERS = ['Meta', 'Control'] as const;

type Modifier = (typeof MODIFIERS)[number];
type HistoryEntry = { id: string; type: string };
type KeydownRecord = { key: string; repeat: boolean; defaultPrevented: boolean };
type ProbeWindow = { __gridJumpKeydowns?: KeydownRecord[] };

async function enableGridJumpFlag(page: Page): Promise<void> {
	await page.route('**/api/v1/flags', async (route) => {
		const response = await route.fetch();
		const body = await response.json();
		const flags = (body.flags ?? []).filter(
			(flag: { flag_id: string }) => flag.flag_id !== 'deck.snapped_jump'
		);
		flags.push({ flag_id: 'deck.snapped_jump', enabled: true, default: false, overridden: true });
		await route.fulfill({ response, json: { ...body, flags } });
	});
}

/** Window capture probe, added before any app script so it runs ahead of the app listener. */
async function installKeydownProbe(page: Page): Promise<void> {
	await page.addInitScript(() => {
		const records: { key: string; repeat: boolean; defaultPrevented: boolean }[] = [];
		(window as unknown as { __gridJumpKeydowns?: typeof records }).__gridJumpKeydowns = records;
		window.addEventListener(
			'keydown',
			(event) => {
				setTimeout(() => {
					records.push({
						key: event.key,
						repeat: event.repeat,
						defaultPrevented: event.defaultPrevented
					});
				}, 0);
			},
			{ capture: true }
		);
	});
}

async function gotoPerformanceWithDeckOne(page: Page): Promise<void> {
	await enableGridJumpFlag(page);
	await installKeydownProbe(page);
	await page.goto('/performance?muted=1');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);
	const stableId = await page
		.locator('[data-testid="track-row"]')
		.first()
		.getAttribute('data-stable-id');
	expect(stableId, 'fixture row has no data-stable-id').toBeTruthy();
	await page.evaluate(async (id) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch({ type: 'load', deck: 1, stable_id: id });
	}, stableId as string);
	await page.waitForFunction(
		(id) => window.musicDjToolsPerformance?.query().decks[1].stable_id === id,
		stableId as string,
		{ timeout: 30_000 }
	);
}

function sequenceOf(entry: HistoryEntry): number {
	const match = /^pc-(\d+)$/.exec(entry.id);
	if (match === null) throw new Error(`unexpected bus history id ${entry.id}`);
	return Number(match[1]);
}

async function readHistory(page: Page): Promise<HistoryEntry[]> {
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC missing');
		return ipc.query().history.map((entry) => ({ id: entry.id, type: entry.type }));
	});
}

async function busMark(page: Page): Promise<number> {
	return Math.max(0, ...(await readHistory(page)).map(sequenceOf));
}

async function gridJumpsSince(page: Page, mark: number): Promise<HistoryEntry[]> {
	return (await readHistory(page)).filter(
		(entry) => sequenceOf(entry) > mark && entry.type === 'grid_jump'
	);
}

/** Body focus: page chrome, where the /performance chords live. */
async function focusBody(page: Page): Promise<void> {
	await page.evaluate(() => {
		if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
		window.getSelection()?.removeAllRanges();
	});
}

/** The last pointer press on deck 1, which is what lets Cmd/Ctrl+A mean "jump back". */
async function pressDeckOneSurface(page: Page): Promise<void> {
	await page.evaluate(() => {
		const deck = document.querySelector('[data-deck-hover="1"]');
		if (deck === null) throw new Error('deck 1 surface missing');
		deck.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
	});
}

/** Hold modifier+key through repeats, then release; returns the probe's records for it. */
async function holdChord(page: Page, modifier: Modifier, key: string): Promise<KeydownRecord[]> {
	await page.evaluate(() => {
		(window as unknown as ProbeWindow).__gridJumpKeydowns?.splice(0);
	});
	await page.keyboard.down(modifier);
	for (let press = 0; press < HELD_KEYDOWNS; press += 1) await page.keyboard.down(key);
	await page.keyboard.up(key);
	await page.keyboard.up(modifier);
	await page.waitForTimeout(SETTLE_MS);
	const records = await page.evaluate(
		() => (window as unknown as ProbeWindow).__gridJumpKeydowns?.slice() ?? []
	);
	return records.filter((record) => record.key.toLowerCase() === key.toLowerCase());
}

test('a held Cmd or Ctrl grid-jump chord dispatches grid_jump only on the initial keydown', async ({
	page
}) => {
	test.setTimeout(120_000);
	await gotoPerformanceWithDeckOne(page);
	for (const modifier of MODIFIERS) {
		for (const key of ['ArrowRight', 'ArrowLeft', 'd']) {
			await focusBody(page);
			const mark = await busMark(page);
			const records = await holdChord(page, modifier, key);
			expect(
				records.filter((record) => record.repeat).length,
				`${modifier}+${key}: the hold produced no repeat keydowns, so it proves nothing`
			).toBeGreaterThan(0);
			expect(
				(await gridJumpsSince(page, mark)).length,
				`if ${modifier}+${key} held sends more than one grid_jump then a held key runs through the track`
			).toBe(1);
		}
	}
});

test('repeat keydowns of a held chord are prevented, so Select all and bookmark do not run', async ({
	page
}) => {
	test.setTimeout(120_000);
	await gotoPerformanceWithDeckOne(page);
	for (const modifier of MODIFIERS) {
		for (const key of ['a', 'd']) {
			await focusBody(page);
			await pressDeckOneSurface(page);
			const mark = await busMark(page);
			const records = await holdChord(page, modifier, key);
			expect(records.length, `${modifier}+${key}: probe saw every keydown`).toBe(HELD_KEYDOWNS);
			expect(
				records.filter((record) => !record.defaultPrevented),
				`if a ${modifier}+${key} keydown is not prevented then the browser default runs mid-hold`
			).toEqual([]);
			expect((await gridJumpsSince(page, mark)).length).toBe(1);
			expect(
				await page.evaluate(() => window.getSelection()?.toString() ?? ''),
				`if ${modifier}+${key} selected page text then Select all ran`
			).toBe('');
		}
	}
});

test('with focus in a text field the grid-jump chord does not dispatch grid_jump', async ({
	page
}) => {
	test.setTimeout(120_000);
	await gotoPerformanceWithDeckOne(page);
	await pressDeckOneSurface(page);
	// A plain text input stands in for any text field: the router keys off the
	// target being text entry, not off which field it is.
	await page.evaluate(() => {
		const field = document.createElement('input');
		field.type = 'text';
		field.value = 'grid jump probe';
		field.setAttribute('data-testid', 'grid-jump-text-probe');
		document.body.appendChild(field);
	});
	const field = page.getByTestId('grid-jump-text-probe');
	for (const modifier of MODIFIERS) {
		for (const key of ['ArrowRight', 'ArrowLeft', 'a', 'd']) {
			await field.focus();
			const mark = await busMark(page);
			const records = await holdChord(page, modifier, key);
			expect(records.length, `${modifier}+${key}: probe saw every keydown`).toBe(HELD_KEYDOWNS);
			expect(
				(await gridJumpsSince(page, mark)).length,
				`if ${modifier}+${key} in a text field sends grid_jump then the field lost its own keys`
			).toBe(0);
		}
	}
});
