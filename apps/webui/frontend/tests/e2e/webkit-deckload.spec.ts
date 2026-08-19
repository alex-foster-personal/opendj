/**
 * WebKit performance-controls suite against the engine-served PRODUCTION build.
 *
 * WHY THIS EXISTS: every chromium + dev-server gate in the repo stayed green
 * while the installed desktop app could not load a single track into a single
 * deck. Two artifact-only faults did it, and dev hid both (vite serves the
 * signalsmith package untransformed): esbuild lowered the worklet processor's
 * class field into a chunk-scope helper the self-stringified Blob never
 * carried, and WKWebView refuses `blob:` URLs for `audioWorklet.addModule`
 * outright. Playwright's webkit shares the WKWebView core, so this suite is
 * the gate that would have caught it.
 *
 * EVERY assertion here is a DOWNSTREAM effect. Reading back the value a
 * control just wrote proves nothing: these tests read the audio path's own
 * telemetry (`last_load_stages` from the engine's load transaction, the
 * presented transport clock, the projected playhead) and the rendered
 * readouts the DJ actually looks at.
 *
 * Library: a throwaway fixture built by the REAL folder ingest over generated
 * click-train audio (support/deckload_fixture.py). Those tracks carry no
 * rekordbox vendor mapping, so `/anlz` serves its documented empty-but-valid
 * payload: no waveform bands and no beatgrid. Nothing here fabricates either.
 *
 * Requirements:
 *
 * - ✔︎ Runs against `build/` served by the engine daemon, never vite.
 * - ✔︎ The double-click load preference is set through the app's own
 *   localStorage persistence, never by stubbing a module.
 * - ✔︎ Deck-load timing comes from the app's own `[perf]` console line AND the
 *   IPC snapshot, so a passing run proves a real worklet was created.
 *
 * Acceptance tests:
 *
 * - [if] worklet creation times out (15s) [then ⛔️] deck 1 leaves the empty state.
 * - [if] the deck reports a load but no stretchCreate stage [then ⛔️] it passes.
 * - [if] the playhead does not advance while playing [then ⛔️] transport passes.
 * - [if] the playhead advances while paused [then ⛔️] transport passes.
 */
import { expect, test, type ConsoleMessage, type Page } from '@playwright/test';

import type {
	PerformanceCommand,
	PerformanceState
} from '../../src/lib/rb/performance-ipc.svelte';
import type { DeckId } from '../../src/lib/rb/types';

/**
 * STRETCH_CREATE_TIMEOUT_MS in stretch-adapter.ts is 15_000: a broken worklet
 * reports exactly that and then fails the load. The fix measured 45ms on the
 * engine-served artifact. 5s is far below the timeout and far above any real
 * creation, so it can only be tripped by the defect, never by a slow runner.
 */
const STRETCH_CREATE_CEILING_MS = 5_000;

/** Quiet enough for a headed run; still non-zero so the graph really runs. */
const E2E_MASTER_VOLUME = 0.1;

/** Fixture files are 60s; a transport sample well inside that. */
const TRANSPORT_SAMPLE_MS = 1_500;

const PREFS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const PERF_LOG_STORAGE_KEY = 'mdt.perfEventLog';

interface PerfConsoleLine {
	readonly text: string;
	readonly stages: Record<string, number>;
}

let perfLines: PerfConsoleLine[] = [];
let pageErrors: string[] = [];

/** Parse `[perf] deck-load ... stretchCreate=45 decodeMix=12` into stages. */
function _parsePerfLine(text: string): PerfConsoleLine | null {
	if (!text.startsWith('[perf] ')) return null;
	const stages: Record<string, number> = {};
	for (const token of text.split(/\s+/)) {
		const eq = token.indexOf('=');
		if (eq <= 0) continue;
		const value = Number(token.slice(eq + 1));
		if (Number.isFinite(value)) stages[token.slice(0, eq)] = value;
	}
	return { text, stages };
}

function _onConsole(message: ConsoleMessage): void {
	const parsed = _parsePerfLine(message.text());
	if (parsed !== null) perfLines.push(parsed);
}

/** Newest `[perf] deck-load` line for one deck, or null. */
function _lastDeckLoadLine(deck: DeckId): PerfConsoleLine | null {
	for (let i = perfLines.length - 1; i >= 0; i--) {
		const line = perfLines[i];
		if (line.text.includes('deck-load') && line.text.includes(`deck=${deck}`)) return line;
	}
	return null;
}

async function _waitForIpc(page: Page): Promise<void> {
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 30_000
	});
}

async function _query(page: Page): Promise<PerformanceState> {
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

async function _dispatch(page: Page, command: PerformanceCommand): Promise<PerformanceState> {
	return page.evaluate(async (message) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.dispatch(message);
	}, command);
}

/** Every visible text node, for the "no failure banner anywhere" assertion. */
async function _bodyText(page: Page): Promise<string> {
	return page.evaluate(() => document.body.innerText);
}

/**
 * Turn BEAT SYNC off on one deck through its real button.
 *
 * Not a convenience: the fixture tracks carry no rekordbox beatgrid, and the
 * engine REFUSES to Beat-Sync-play a gridless deck ("beat grid must contain at
 * least 2 beats"). Turning it off is the same thing a DJ does with an
 * unanalysed track, and it is what makes every downstream transport assertion
 * in this file measure transport rather than that refusal.
 */
async function _turnOff(page: Page, deck: DeckId, control: string): Promise<void> {
	const button = page.locator(
		`section.rb-deck[data-deck="${deck}"] [data-performance-control="${control}"]`
	);
	if ((await button.getAttribute('data-state')) === 'off') return;
	await button.click();
	await expect(button).toHaveAttribute('data-state', 'off');
}

/**
 * BEAT SYNC and QUANTIZE both hard-require a real PQTZ grid: the engine
 * refuses "play Beat Sync", "pause cue" and "cueJump quantize" on a gridless
 * deck ("beat grid must contain at least 2 beats"). The fixture library has no
 * rekordbox analysis, so both go off before anything loads - exactly what a DJ
 * does with an unanalysed track, and what makes the transport assertions below
 * measure transport instead of that refusal.
 */
async function _prepareGridlessDeck(page: Page, deck: DeckId): Promise<void> {
	await _turnOff(page, deck, 'beat-sync');
	await _turnOff(page, deck, 'quantize');
}

async function _openAllTracks(page: Page): Promise<void> {
	await page.getByText('All Tracks', { exact: true }).first().click();
	await expect(page.locator('tbody tr[data-stable-id]').first()).toBeVisible({
		timeout: 30_000
	});
}

/**
 * Double-click the title cell of row `index`. The app's own smart-load picks
 * the deck (least-recently-loaded of CH1/CH2), which is the flow a DJ uses.
 */
async function _dblClickLoad(page: Page, index: number): Promise<string> {
	const row = page.locator('tbody tr[data-stable-id]').nth(index);
	const stableId = await row.getAttribute('data-stable-id');
	if (stableId === null) throw new Error(`row ${index} has no data-stable-id`);
	await row.locator('td.c-title').dblclick();
	return stableId;
}

/** Wait until the deck's PRESENTED transport state matches `audible`. */
async function _waitForAudible(page: Page, deck: DeckId, audible: boolean): Promise<void> {
	await page.waitForFunction(
		({ deckId, expected }) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) return false;
			const state = ipc.query().decks[deckId];
			return (
				state.audible === expected &&
				!state.transport_pending &&
				state.transport_clock.presented_revision === state.transport_clock.desired_revision
			);
		},
		{ deckId: deck, expected: audible },
		{ timeout: 30_000 }
	);
}

/** Playhead movement over one wall-clock sample window, in ms. */
async function _sampleAdvanceMs(page: Page, deck: DeckId): Promise<number> {
	const before = (await _query(page)).decks[deck].position_ms;
	await page.waitForTimeout(TRANSPORT_SAMPLE_MS);
	return (await _query(page)).decks[deck].position_ms - before;
}

/** DeckHeader's MM:SS.d formatter, mirrored so the readout can be asserted. */
function _clockText(positionMs: number): string {
	const totalS = Math.max(0, positionMs) / 1000;
	const m = Math.floor(totalS / 60);
	const s = Math.floor(totalS % 60);
	const tenths = Math.floor((totalS * 10) % 10);
	return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${tenths}`;
}

async function _waitForDeckLoaded(page: Page, deck: DeckId): Promise<void> {
	await page.waitForFunction(
		(deckId) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) return false;
			return ipc.query().decks[deckId].stable_id !== null;
		},
		deck,
		{ timeout: 45_000 }
	);
}

test.describe.configure({ mode: 'serial' });

test.describe('webkit performance controls on the engine-served build', () => {
	let page: Page;

	test.beforeAll(async ({ browser }) => {
		page = await browser.newPage();
		page.on('console', _onConsole);
		page.on('pageerror', (error) => pageErrors.push(String(error)));
		// The app's REAL persistence, not a stub: a prefs blob it would have
		// written itself, so the double-click load takes the deterministic
		// no-confirm path. The perf ring is cleared so a stale entry from an
		// earlier run cannot be mistaken for this run's telemetry.
		await page.addInitScript(
			({ prefsKey, perfKey }) => {
				window.localStorage.setItem(
					prefsKey,
					JSON.stringify({
						hide_broken_links: false,
						confirm: { dblclick_load_play: false }
					})
				);
				window.localStorage.removeItem(perfKey);
			},
			{ prefsKey: PREFS_STORAGE_KEY, perfKey: PERF_LOG_STORAGE_KEY }
		);
		await page.goto('/performance');
		await _waitForIpc(page);
		await _dispatch(page, { type: 'master_volume', value: E2E_MASTER_VOLUME });
		await _prepareGridlessDeck(page, 1);
		await _prepareGridlessDeck(page, 2);
		await _openAllTracks(page);
	});

	test.afterAll(async () => {
		await page.close();
	});

	test('double-click loads deck 1 and deck 2 with a real worklet', async () => {
		const firstId = await _dblClickLoad(page, 0);
		await _waitForDeckLoaded(page, 1);
		const secondId = await _dblClickLoad(page, 1);
		await _waitForDeckLoaded(page, 2);

		const state = await _query(page);
		expect(state.decks[1].stable_id).toBe(firstId);
		expect(state.decks[2].stable_id).toBe(secondId);

		// The empty state the broken artifact never left.
		await expect(page.locator('section.rb-deck[data-deck="1"] .title')).not.toHaveText(
			'No track loaded'
		);
		await expect(page.locator('section.rb-deck[data-deck="2"] .title')).not.toHaveText(
			'No track loaded'
		);

		const body = await _bodyText(page);
		expect(body).not.toContain('StretchCommandTimeoutError');
		expect(body).not.toContain('load failed');

		// Downstream proof that a worklet was really created, from the app's
		// own load-transaction telemetry - both the console line and the IPC
		// snapshot, because either alone could be a stale ring entry.
		for (const deck of [1, 2] as const) {
			const line = _lastDeckLoadLine(deck);
			expect(line, `no [perf] deck-load console line for deck ${deck}`).not.toBeNull();
			const consoleStretch = line?.stages.stretchCreate;
			expect(
				consoleStretch,
				`deck ${deck} console line has no stretchCreate stage: ${line?.text}`
			).toBeGreaterThanOrEqual(0);
			expect(consoleStretch).toBeLessThan(STRETCH_CREATE_CEILING_MS);

			const stages = state.decks[deck].last_load_stages;
			expect(stages, `deck ${deck} reported no load stages`).not.toBeNull();
			expect(stages?.stretchCreate).toBeLessThan(STRETCH_CREATE_CEILING_MS);
			expect(state.decks[deck].processor_error).toBeNull();
			expect(state.decks[deck].command_error).toBeNull();
		}
	});

	test('the wave row exposes the decoded duration of the loaded audio', async () => {
		// The fixture has no rekordbox ANLZ, so there are no waveform BANDS to
		// paint (see the file header). What the wave row still derives from the
		// real decoded AudioBuffer is its seek range, and that is the honest
		// downstream effect available here: aria-valuemax is the deck duration
		// the decode produced, not anything the API handed over.
		const state = await _query(page);
		const durationMs = state.decks[1].duration_ms;
		expect(durationMs, 'deck 1 has no decoded duration').not.toBeNull();
		// 60s of generated audio, decoded. Bounds, not an exact match: the
		// decoder owns the last few frames.
		expect(durationMs).toBeGreaterThan(59_000);
		expect(durationMs).toBeLessThan(61_000);

		const canvas = page.locator('.rb-waverow[data-deck="1"] canvas');
		await expect(canvas).toHaveAttribute('aria-valuemax', String(durationMs));
	});

	test('play advances the playhead, pause holds it, play resumes it', async () => {
		// Double-click load is a load+PLAY, so deck 1 is already running the
		// real audio clock here. That is the state under test, not a setup step.
		await _waitForAudible(page, 1, true);
		const advancedFromLoad = await _sampleAdvanceMs(page, 1);
		expect(advancedFromLoad).toBeGreaterThan(TRANSPORT_SAMPLE_MS * 0.5);
		expect(advancedFromLoad).toBeLessThan(TRANSPORT_SAMPLE_MS * 1.5);

		const playButton = page.locator(
			'section.rb-deck[data-deck="1"] [data-performance-control="play"]'
		);
		await playButton.click();
		await _waitForAudible(page, 1, false);

		const heldMs = await _sampleAdvanceMs(page, 1);
		expect(Math.abs(heldMs)).toBeLessThan(50);
		const paused = await _query(page);
		expect(paused.decks[1].playing).toBe(false);
		// The presented clock is the engine's own record of what the audio
		// path actually produced, not the UI's optimistic intent.
		expect(paused.decks[1].transport_clock.presented_revision).toBe(
			paused.decks[1].transport_clock.desired_revision
		);
		// The rendered readout a DJ reads must agree with the audio clock.
		await expect(page.locator('section.rb-deck[data-deck="1"] .elapsed')).toHaveText(
			_clockText(paused.decks[1].position_ms)
		);

		await playButton.click();
		await _waitForAudible(page, 1, true);
		const resumedMs = await _sampleAdvanceMs(page, 1);
		expect(resumedMs).toBeGreaterThan(TRANSPORT_SAMPLE_MS * 0.5);
		expect(resumedMs).toBeLessThan(TRANSPORT_SAMPLE_MS * 1.5);

		await playButton.click();
		await _waitForAudible(page, 1, false);
	});

	test('no uncaught page errors were raised during the run', () => {
		expect(pageErrors, pageErrors.join('\n')).toHaveLength(0);
	});
});
