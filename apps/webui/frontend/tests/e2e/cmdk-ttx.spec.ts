/**
 * requirement: CMDK-9468
 * Issue #9468: time-to-execute for 20 scripted Cmd K commands on the real engine.
 *
 * Regression lines:
 * - if any scripted command does not reach the performance command history then broken
 * - if median Cmd-K-to-bus ms exceeds 1500 then broken (first ratchet)
 * - if median keystrokes exceed 8 then broken (first ratchet)
 *
 * Run: pnpm exec playwright test --config tests/e2e/playwright.cmdk-ttx.config.ts
 */
import { expect, test, type Page } from '@playwright/test';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { DeckId } from '../../src/lib/rb/deck-id';

declare global {
	interface Window {
		__cmdkTtxT0?: number;
	}
}

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const OUT_PATH =
	process.env.CMDK_TTX_OUT ?? join(FRONTEND_ROOT, 'tests', 'e2e', 'fixtures', 'cmdk-ttx.json');
const TYPING_DELAY_MS = 0;
/** first ratchet; replace with the first measured run */
export const MEDIAN_MS_MAX = 1500;
/** first ratchet; replace with the first measured run */
export const MEDIAN_KEYSTROKES_MAX = 8;

const CHORD = process.platform === 'darwin' ? 'Meta+k' : 'Control+k';

type StepDef = {
	command: string;
	deck: DeckId | null;
	query: string;
	busType: string;
	rowIdPrefix: string;
	fixture_id: string | null;
};

const STEPS: StepDef[] = [
	{
		command: 'deck.beat_sync',
		deck: 2,
		query: 'match',
		busType: 'beat_sync',
		rowIdPrefix: 'palette:deck.beat_sync@2',
		fixture_id: 'beat-sync'
	},
	{
		command: 'deck.beat_sync',
		deck: 3,
		query: 'match',
		busType: 'beat_sync',
		rowIdPrefix: 'palette:deck.beat_sync@3',
		fixture_id: 'beat-sync'
	},
	{
		command: 'deck.beat_sync',
		deck: 4,
		query: 'match',
		busType: 'beat_sync',
		rowIdPrefix: 'palette:deck.beat_sync@4',
		fixture_id: 'beat-sync'
	},
	{
		command: 'deck.master',
		deck: 2,
		query: 'make',
		busType: 'master',
		rowIdPrefix: 'palette:deck.master@2',
		fixture_id: 'master-deck'
	},
	{
		command: 'deck.master',
		deck: 1,
		query: 'make',
		busType: 'master',
		rowIdPrefix: 'palette:deck.master@1',
		fixture_id: 'master-deck'
	},
	{
		command: 'deck.loop.exit',
		deck: 1,
		query: 'exit',
		busType: 'loop',
		rowIdPrefix: 'palette:deck.loop.exit@1',
		fixture_id: 'loop-exit'
	},
	{
		command: 'deck.loop.exit',
		deck: 2,
		query: 'exit',
		busType: 'loop',
		rowIdPrefix: 'palette:deck.loop.exit@2',
		fixture_id: 'loop-exit'
	},
	{
		command: 'deck.loop.exit',
		deck: 3,
		query: 'exit',
		busType: 'loop',
		rowIdPrefix: 'palette:deck.loop.exit@3',
		fixture_id: 'loop-exit'
	},
	{
		command: 'deck.play',
		deck: 3,
		query: 'space',
		busType: 'play',
		rowIdPrefix: 'palette:deck.play@3:',
		fixture_id: null
	},
	{
		command: 'deck.play',
		deck: 3,
		query: 'paus',
		busType: 'play',
		rowIdPrefix: 'palette:deck.play@3:',
		fixture_id: null
	},
	{
		command: 'waveform.zoom_in',
		deck: null,
		query: 'detail',
		busType: 'wave_zoom_step',
		rowIdPrefix: 'palette:waveform.zoom_in',
		fixture_id: null
	},
	{
		command: 'waveform.zoom_out',
		deck: null,
		query: 'bars',
		busType: 'wave_zoom_step',
		rowIdPrefix: 'palette:waveform.zoom_out',
		fixture_id: null
	},
	{
		command: 'playlist.undo',
		deck: null,
		query: 'undo',
		busType: 'playlist_undo',
		rowIdPrefix: 'palette:playlist.undo',
		fixture_id: null
	},
	{
		command: 'playlist.redo',
		deck: null,
		query: 'redo',
		busType: 'playlist_redo',
		rowIdPrefix: 'palette:playlist.redo',
		fixture_id: null
	},
	{
		command: 'tech_mode.toggle',
		deck: null,
		query: 'tech',
		busType: 'tech_mode_toggle',
		rowIdPrefix: 'palette:tech_mode.toggle',
		fixture_id: null
	},
	{
		command: 'tech_mode.toggle',
		deck: null,
		query: 'tech',
		busType: 'tech_mode_toggle',
		rowIdPrefix: 'palette:tech_mode.toggle',
		fixture_id: null
	},
	{
		command: 'eject',
		deck: 1,
		query: 'ej1',
		busType: 'unload',
		rowIdPrefix: 'eject-1:',
		fixture_id: 'eject-1'
	},
	{
		command: 'eject',
		deck: 2,
		query: 'ej2',
		busType: 'unload',
		rowIdPrefix: 'eject-2:',
		fixture_id: 'eject-2'
	},
	{
		command: 'eject',
		deck: 3,
		query: 'ej3',
		busType: 'unload',
		rowIdPrefix: 'eject-3:',
		fixture_id: 'eject-3'
	},
	{
		command: 'eject',
		deck: 4,
		query: 'ej4',
		busType: 'unload',
		rowIdPrefix: 'eject-4:',
		fixture_id: 'eject-4'
	}
];

const TRACK_IDS = ['cmdk-ttx-01', 'cmdk-ttx-02', 'cmdk-ttx-03', 'cmdk-ttx-04'] as const;

interface RowResult {
	step: number;
	command: string;
	deck: DeckId | null;
	fixture_id: string | null;
	query: string;
	keys: string[];
	keystrokes: number;
	arrows: number;
	ms: number | null;
	bus: { id: string; type: string } | null;
	reached_bus: boolean;
	reason: string | null;
}

function median(values: readonly number[]): number {
	const sorted = [...values].sort((a, b) => a - b);
	const mid = Math.floor(sorted.length / 2);
	return sorted.length % 2 === 1 ? sorted[mid]! : (sorted[mid - 1]! + sorted[mid]!) / 2;
}

async function closeBar(page: Page): Promise<void> {
	for (let attempt = 0; attempt < 3; attempt++) {
		const open = await page.evaluate(
			() => window.musicDjToolsPerformance?.query().cmdk?.open ?? false
		);
		if (!open) return;
		await page.keyboard.press('Escape');
		await page.waitForTimeout(50);
	}
}

async function warmBar(page: Page): Promise<void> {
	await expect(async () => {
		await page.keyboard.press(CHORD);
		await page.waitForFunction(
			() => window.musicDjToolsPerformance?.query().cmdk?.open === true,
			undefined,
			{
				timeout: 2_000
			}
		);
	}).toPass({ timeout: 30_000 });
	await closeBar(page);
}

async function runStep(page: Page, step: StepDef, stepNum: number): Promise<RowResult> {
	const keys: string[] = [];
	let arrows = 0;

	await closeBar(page);
	const before = await page.evaluate(() => {
		const hist = window.musicDjToolsPerformance!.query().history;
		return { beforeId: hist.at(-1)?.id ?? null, beforeLen: hist.length };
	});
	await page.evaluate(() => {
		delete window.__cmdkTtxT0;
	});

	await page.keyboard.press(CHORD);
	keys.push(CHORD);
	await page.waitForFunction(
		() => window.musicDjToolsPerformance?.query().cmdk?.open === true,
		undefined,
		{
			timeout: 10_000
		}
	);

	if (step.deck !== null && step.command !== 'eject') {
		await page.keyboard.press(`Alt+${step.deck}`);
		keys.push(`Alt+${step.deck}`);
	}

	await page.keyboard.type(step.query, { delay: TYPING_DELAY_MS });
	for (const char of step.query) {
		keys.push(char === ' ' ? 'Space' : char);
	}

	const barTarget = step.command === 'eject' ? null : step.deck;
	let rowFound = false;
	let selected = 0;
	let index = -1;
	try {
		await page.waitForFunction(
			({ prefix, query, deck }) => {
				const q = window.musicDjToolsPerformance?.query();
				const cmdk = q?.cmdk;
				if (!cmdk?.open || cmdk.query !== query) return false;
				if (cmdk.collection?.status === 'loading') return false;
				if (deck !== null && cmdk.target !== deck) return false;
				return (cmdk.rows ?? []).some((row) => row.id.startsWith(prefix));
			},
			{ prefix: step.rowIdPrefix, query: step.query, deck: barTarget },
			{ polling: 'raf', timeout: 5_000 }
		);
		const pos = await page.evaluate(
			({ prefix }) => {
				const cmdk = window.musicDjToolsPerformance!.query().cmdk!;
				const rows = cmdk.rows;
				const i = rows.findIndex((row) => row.id.startsWith(prefix));
				return { selected: cmdk.selected, index: i };
			},
			{ prefix: step.rowIdPrefix }
		);
		selected = pos.selected;
		index = pos.index;
		rowFound = index >= 0;
	} catch {
		rowFound = false;
	}

	if (rowFound && index >= 0) {
		const delta = index - selected;
		const arrowKey = delta > 0 ? 'ArrowDown' : 'ArrowUp';
		const presses = Math.abs(delta);
		for (let n = 0; n < presses; n++) {
			await page.keyboard.press(arrowKey);
			keys.push(arrowKey);
			arrows++;
		}
	}

	await page.keyboard.press('Enter');
	keys.push('Enter');

	let ms: number | null = null;
	let bus: { id: string; type: string } | null = null;
	let reached = false;
	let reason: string | null = null;

	if (!rowFound) {
		reason = 'row-missing';
	} else {
		try {
			const arrival = await page.waitForFunction(
				({ beforeLen, type, deck }) => {
					const q = window.musicDjToolsPerformance!.query();
					const hist = q.history;
					if (hist.length <= beforeLen) return null;
					const entry = hist[hist.length - 1]!;
					if (entry.type !== type) return null;
					if (deck !== null) {
						const lastId = q.decks[deck as 1 | 2 | 3 | 4].last_command_id;
						if (lastId !== entry.id) return null;
					}
					return performance.now();
				},
				{ beforeLen: before.beforeLen, type: step.busType, deck: step.deck },
				{ polling: 'raf', timeout: 5_000 }
			);
			const arrivalMs = await arrival.jsonValue();
			const t0 = await page.evaluate(() => window.__cmdkTtxT0 ?? null);
			if (typeof arrivalMs === 'number' && typeof t0 === 'number') {
				ms = arrivalMs - t0;
			}
			const busEntry = await page.evaluate(() => {
				const hist = window.musicDjToolsPerformance!.query().history;
				const last = hist.at(-1);
				return last ? { id: last.id, type: last.type } : null;
			});
			bus = busEntry;
			reached = bus !== null && bus.type === step.busType;
			if (!reached) reason = 'no-bus-entry';
		} catch {
			reason = 'no-bus-entry';
		}
	}

	return {
		step: stepNum,
		command: step.command,
		deck: step.deck,
		fixture_id: step.fixture_id,
		query: step.query,
		keys,
		keystrokes: keys.length,
		arrows,
		ms,
		bus,
		reached_bus: reached,
		reason: reached ? null : reason
	};
}

test.describe.configure({ mode: 'serial' });
test.skip(
	process.platform === 'win32',
	'POSIX-only: guardedWebServerCommand launches /bin/sh and .venv/bin/python'
);

test.describe('Cmd K time-to-execute (CMDK-9468)', () => {
	let page: Page;

	test.beforeAll(async ({ browser }) => {
		test.setTimeout(180_000);
		page = await browser.newPage();
		await page.addInitScript(() => {
			window.addEventListener(
				'keydown',
				(event) => {
					if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
						window.__cmdkTtxT0 = performance.now();
					}
				},
				true
			);
		});
		await page.goto('/performance', { timeout: 170_000 });
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
			timeout: 170_000
		});

		const warmDecks: readonly DeckId[] = [1, 2, 3, 4];
		for (const deck of warmDecks) {
			const stableId = TRACK_IDS[deck - 1]!;
			await page.evaluate(
				([d, track]) =>
					window.musicDjToolsPerformance!.dispatch({ type: 'load', deck: d, stable_id: track }),
				[deck, stableId] as const
			);
			await expect
				.poll(() =>
					page.evaluate(
						(id: DeckId) => window.musicDjToolsPerformance!.query().decks[id].stable_id,
						deck
					)
				)
				.toBe(stableId);
		}
		await page.evaluate(() =>
			window.musicDjToolsPerformance!.dispatch({ type: 'master', deck: 1 })
		);
		await warmBar(page);
	});

	test.afterAll(async () => {
		await page.close();
	});

	test('20 scripted commands reach the bus within keystroke and time ratchets', async () => {
		test.setTimeout(180_000);
		const rows: RowResult[] = [];

		for (let i = 0; i < STEPS.length; i++) {
			rows.push(await runStep(page, STEPS[i]!, i + 1));
		}

		const fixture = JSON.parse(
			readFileSync(new URL('../unit/fixtures/palette-queries.json', import.meta.url), 'utf8')
		) as { commands: { id: string }[]; queries: { expected: string }[] };
		const fixtureCommandIds = new Set([
			...fixture.commands.map((command) => command.id),
			...fixture.queries.map((row) => row.expected)
		]);

		const missed = rows.filter((row) => !row.reached_bus).map((row) => row.step);
		const reachedMs = rows
			.filter((row) => row.reached_bus && row.ms !== null)
			.map((row) => row.ms!);
		const medianMs = reachedMs.length > 0 ? median(reachedMs) : 0;
		const medianKeystrokes = median(rows.map((row) => row.keystrokes));

		const report = {
			schema: 1,
			generated_at: new Date().toISOString(),
			issue: 9468,
			typing_delay_ms: TYPING_DELAY_MS,
			warm_bar: true,
			ratchet: { median_ms_max: MEDIAN_MS_MAX, median_keystrokes_max: MEDIAN_KEYSTROKES_MAX },
			rows,
			median: { ms: medianMs, keystrokes: medianKeystrokes },
			reached: rows.filter((row) => row.reached_bus).length,
			missed
		};

		mkdirSync(dirname(OUT_PATH), { recursive: true });
		writeFileSync(OUT_PATH, `${JSON.stringify(report, null, '\t')}\n`, 'utf8');
		// eslint-disable-next-line no-console
		console.log(
			`[cmdk-ttx] MEDIANS ${JSON.stringify(report.median)} reached ${report.reached}/20 -> ${OUT_PATH}`
		);

		expect(rows.length, 'row count').toBe(20);
		expect(STEPS.length, 'step count').toBe(20);

		for (const row of rows) {
			if (row.fixture_id !== null) {
				expect(
					fixtureCommandIds.has(row.fixture_id),
					`fixture_id ${row.fixture_id} missing from palette-queries.json`
				).toBe(true);
			}
		}

		expect(missed, `commands that missed the bus: ${missed.join(', ')}`).toEqual([]);
		expect(medianMs, `median ms ${medianMs} above ${MEDIAN_MS_MAX}`).toBeLessThanOrEqual(
			MEDIAN_MS_MAX
		);
		expect(
			medianKeystrokes,
			`median keystrokes ${medianKeystrokes} above ${MEDIAN_KEYSTROKES_MAX}`
		).toBeLessThanOrEqual(MEDIAN_KEYSTROKES_MAX);
	});
});
