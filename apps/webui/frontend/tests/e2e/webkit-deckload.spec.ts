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
 * audio (support/deckload_fixture.py) - a 2 kHz sustained tone with a 100 Hz
 * beat pulse. The tone is the SPECTRAL LANDMARK the tempo/key/master-tempo
 * tests measure through the app's own analyser. Those tracks carry no
 * rekordbox vendor mapping, so `/anlz` serves its documented empty-but-valid
 * payload: no waveform bands and no beatgrid. Nothing here fabricates either.
 *
 * WHAT THE MISSING BEATGRID COSTS, stated rather than worked around: BEAT SYNC
 * and QUANTIZE are switched off through their real buttons, because the engine
 * refuses "play Beat Sync", "pause cue" and "cueJump quantize" without a grid.
 * The UI's loop control is a BEAT loop (`engageBeatLoop` requires a grid
 * unconditionally), so the loop-wrap assertion drives the millisecond loop
 * through the agent-native IPC endpoint the house rules require every UI
 * control to have, and a separate test pins the UI button's honest-inert
 * contract. No beat-loop, key-sync or quantized-seek behaviour is asserted.
 *
 * PLAYHEAD RUNWAY, and the bug that bought it: the fixture audio is 60s and
 * deck 1 plays continuously from the double-click load onward, so the suite's
 * own cumulative playing time - about 68s on a fast machine - used to walk the
 * playhead off the END of the track partway through. Nothing was broken when
 * it did: auto-play looked for a successor, correctly found none (both fixture
 * tracks are already loaded), and the deck stopped. Every later
 * `_waitForAudible` then timed out against a deck that was loaded and simply
 * out of audio, and WHICH test wore the failure depended on nothing but machine
 * speed - it read as the tempo test on one machine, the loop test on another,
 * and play/pause on a slower run of the same worktree. A beforeEach now returns
 * deck 1 to zero, so every test gets the whole 60s regardless of what ran
 * before it, and `_waitForAudible` states the playhead it gave up on.
 *
 * SELECTOR DISCIPLINE, all of it learned by something breaking:
 *
 * - Controls are addressed by `data-performance-control`, never by `title`.
 *   Titles are REWRITTEN per state ("no track loaded" -> "Play from current
 *   playhead" -> "Pause - also stores the memory cue here"), so a title-bound
 *   selector silently stops matching the moment a deck starts playing and reads
 *   as a missing control. `aria-label` is the stable fallback where no data
 *   attribute exists. The one `title` assertion below is on CONTENT, not a
 *   selector, and pins a state this fixture can never leave.
 * - Deck panels are `section.rb-deck[data-deck="N"]`: a bare `[data-deck]`
 *   matches EIGHT elements (four waverow lanes plus four deck panels), and DOM
 *   order is 1, 3, 2, 4, so nothing may be addressed by position.
 * - Per-row controls (e.g. `button[title="Load onto deck N"]`) exist in EVERY
 *   row and are visible only on the hovered one, so they must be scoped to a
 *   row locator and never reached with a global `.first()`.
 *
 * HONEST DENOMINATORS: the library here is GENERATED, so every row has real,
 * readable audio and `file_exists` is true for all of them. That is deliberate.
 * Sampling the top of a real snapshot playlist would test the fixture's
 * brokenness rather than the app - one such playlist is 18 of 28 available with
 * rows 0-3 all missing.
 *
 * Requirements:
 *
 * - ✔︎ Runs against `build/` served by the engine daemon, never vite.
 * - ✔︎ The double-click load preference is set through the app's own
 *   localStorage persistence, never by stubbing a module.
 * - ✔︎ Deck-load timing comes from the app's own `[perf]` console line AND the
 *   IPC snapshot, so a passing run proves a real worklet was created.
 * - ✔︎ Every rate and every frequency is read from ONE named deck, never from a
 *   flat scrape of the page, so a reading can always be attributed.
 * - ✔︎ Every test starts deck 1 from a known playhead, so no test inherits how
 *   long the tests before it happened to take.
 *
 * Acceptance tests:
 *
 * - [if] worklet creation times out (15s) [then ⛔️] deck 1 leaves the empty state.
 * - [if] the deck reports a load but no stretchCreate stage [then ⛔️] it passes.
 * - [if] the playhead does not advance while playing [then ⛔️] transport passes.
 * - [if] the playhead advances while paused [then ⛔️] transport passes.
 * - [if] a tempo change does not change the playhead RATE [then ⛔️] tempo passes.
 * - [if] MASTER TEMPO fails to hold pitch under a tempo change [then ⛔️] it passes.
 * - [if] MASTER TEMPO off does not shift pitch with tempo [then ⛔️] it passes.
 * - [if] a key nudge does not move the tone by a semitone [then ⛔️] key passes.
 * - [if] CUE does not return the playhead to the cue point [then ⛔️] cue passes.
 * - [if] the playhead leaves the loop window [then ⛔️] the loop test passes.
 * - [if] the looping playhead stops advancing for a second, at any point in the
 *   observation, [then ⛔️] the loop test passes.
 * - [if] a deck refuses a dragover [then ⛔️] the drag test passes.
 * - [if] a deck ignores a drop whose transfer is empty [then ⛔️] it passes.
 * - [if] a test inherits the playhead the one before it left [then ⛔️] this
 *   suite is independent of machine speed.
 * - [if] a seek landing is asserted while the playhead is still moving
 *   [then ⛔️] a poller that stalls past the window reports that seek honestly.
 * - [if] a wait for audible gives up without naming the playhead and duration
 *   it gave up on [then ⛔️] the next reader can tell a dry deck from a dead one.
 */
import { expect, test, type ConsoleMessage, type Page } from '@playwright/test';

import type {
	PerformanceCommand,
	PerformanceState
} from '../../src/lib/rb/performance-ipc.svelte';
import type { DeckId } from '../../src/lib/rb/deck-slots';
import { describeStalledLoopSlices, findStalledLoopSlices } from './support/loop-continuity';

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

/** Rate window. Long enough that scheduler jitter is a small fraction of it. */
const RATE_WINDOW_MS = 2_000;
/** A deck must hold "loaded and not pending" this many polls to count as settled. */
const DECK_SETTLE_POLLS = 3;
const DECK_SETTLE_POLL_MS = 100;
const DECK_SETTLE_TIMEOUT_MS = 45_000;

/**
 * Band the dominant-bin search runs over, matching MEASUREMENT_BAND_HZ in
 * support/deckload_fixture.py. It holds the 2 kHz tone across everything this
 * suite drives (1888 Hz at -1 semitone to 2320 Hz at +16%) and excludes the
 * 100 Hz beat pulse and its low harmonics.
 */
const MEASUREMENT_BAND_HZ: readonly [number, number] = [800, 8_000];

/** Captures per measurement. The analyser has smoothing 0, so each is
 * independent; the spread between them is asserted rather than averaged away. */
const SPECTRUM_SAMPLES = 5;

/** Max spread across those captures before the measurement is called unstable.
 * One analyser bin is ~10.8 Hz, so this is a little over two bins. */
const SPECTRUM_SPREAD_TOLERANCE_HZ = 25;

/** Ceiling on the settle phase before a measurement. A tempo change reaches the
 * analyser in a few hundred ms; anything near this is a stuck graph. */
const SPECTRUM_SETTLE_TIMEOUT_MS = 8_000;

/** Fractional tolerance on a frequency ratio assertion (~1.4 bins at 2 kHz). */
const FREQUENCY_RATIO_TOLERANCE = 0.0075;

/** The pitch range this suite selects, and the ratio its top of travel means. */
const PITCH_RANGE_PCT = 16;
const PITCH_MAX_RATIO = 1 + PITCH_RANGE_PCT / 100;

const SEMITONE_RATIO = Math.pow(2, 1 / 12);

/** Loop window under test, and how long the playhead is watched inside it. */
const LOOP_LENGTH_MS = 1_200;
const LOOP_OBSERVE_MS = 5_000;
const LOOP_SAMPLE_INTERVAL_MS = 100;
/** Slack on the loop edges: one scheduler quantum, not a free pass. */
const LOOP_EDGE_TOLERANCE_MS = 150;
/**
 * Continuity slice for the "still moving" assertion. Shorter than the loop so a
 * slice's wrap-aware advance is unambiguous, and ten sample intervals long so a
 * throttled presentation frame is not a failure. The floor is a quarter of the
 * slice: a stalled or clamped cursor advances exactly zero, so the floor only
 * has to clear noise. See tests/e2e/support/loop-continuity.ts.
 */
const LOOP_CONTINUITY_SLICE_MS = 1_000;
const LOOP_CONTINUITY_MIN_ADVANCE_MS = 250;

/**
 * Slack on a seek landing, measured on a STOPPED deck.
 *
 * This used to be 2_000 and to mean "how far the playhead may already have
 * traveled", which made the assertion a WINDOW on a value that was still
 * moving. `_seek` now stops the deck first, so the landing is a resting
 * position and this covers the presented clock's own rounding and nothing
 * else. It is not a race window, and widening it would not buy one (#689).
 */
const SEEK_LANDING_TOLERANCE_MS = 100;

/**
 * Poll interval for every wait in this file that reads a value the AUDIO PATH
 * moves on its own.
 *
 * Playwright polls on `requestAnimationFrame` by default. rAF is throttled on
 * a loaded runner and suspended outright on a hidden page, so an rAF-polled
 * wait can go seconds between observations - and the engine's own presentation
 * clock only advances inside that same tick (see `_presentationPending` in
 * audio-engine.svelte.ts). A wall-clock interval decouples the OBSERVER from
 * the thing it is observing. Waits whose predicate latches (a stable_id
 * appearing, a pitch the test itself just set) are left on rAF: a slow poll
 * only delays those, it cannot make them miss.
 */
const TRANSPORT_POLL_MS = 100;

/**
 * Playhead distance the CUE test travels before stamping its cue point. Well
 * past the 1s floor its own assertion needs, and nowhere near the 60s fixture.
 */
const CUE_RUNWAY_MS = 3_000;

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
 * BEAT SYNC, QUANTIZE and SLIP off on one deck, through their real buttons.
 *
 * Not a convenience. BEAT SYNC and QUANTIZE hard-require a real PQTZ grid: the
 * engine refuses "play Beat Sync", "pause cue" and "cueJump quantize" on a
 * gridless deck ("beat grid must contain at least 2 beats"). The fixture
 * library has no rekordbox analysis, so both go off before anything loads -
 * exactly what a DJ does with an unanalysed track, and what makes the
 * transport assertions below measure transport instead of that refusal. SLIP
 * goes off so the loop test measures the loop rather than slip's shadow
 * playhead.
 */
async function _prepareGridlessDeck(page: Page, deck: DeckId): Promise<void> {
	await _setToggle(page, deck, 'beat-sync', 'off');
	await _setToggle(page, deck, 'quantize', 'off');
	await _setToggle(page, deck, 'slip', 'off');
}

const TRACK_ROW = '[data-testid="track-row"]';

/**
 * TRACK_STABLE_MIME from src/lib/rb/track-drag.svelte.ts, restated rather than
 * imported: that module is a runes module and evaluating it in this node
 * process would fail. The empty-transfer test would notice a drift anyway - it
 * asserts an EMPTY payload, and a wrong MIME here would read empty for the
 * wrong reason, which is why the payload-carrying test asserts the stable id
 * through this same constant.
 */
const TRACK_STABLE_MIME = 'application/x-mdt-stable-id';

async function _openAllTracks(page: Page): Promise<void> {
	await page.getByText('All Tracks', { exact: true }).first().click();
	await expect(page.locator(TRACK_ROW).first()).toBeVisible({ timeout: 30_000 });
}

/**
 * Double-click the title cell of row `index`. The app's own smart-load picks
 * the deck (least-recently-loaded of CH1/CH2), which is the flow a DJ uses.
 */
/** What a dispatched drag gesture observed, read from the events themselves. */
interface DragGestureReport {
	dragOverAccepted: boolean;
	typesDuringDragOver: string[];
	payloadOnDrop: string;
}

/**
 * Drag a library row onto a deck by DISPATCHING the HTML5 sequence.
 *
 * Playwright's mouse API cannot do this: moving the mouse with a button held
 * produces pointer events, and no browser synthesizes a native drag from them,
 * so `dragTo` and manual mouse-drags never reach an ondrop handler. The events
 * are therefore constructed here and dispatched at the app's OWN handlers -
 * the row's ondragstart, the deck's ondragover and ondrop. Nothing in the app
 * is stubbed: the drag begins because the row's real dragstart handler ran.
 *
 * `carryPayload: false` offers the deck a transfer that carries nothing, which
 * is what WKWebView hands a drop target in protected drag mode.
 */
async function _dragRowToDeck(
	page: Page,
	rowIndex: number,
	deck: DeckId,
	options: { carryPayload: boolean }
): Promise<DragGestureReport> {
	return page.evaluate(
		({ rowSelector, index, deckId, carryPayload, mime }) => {
			const row = document.querySelectorAll(rowSelector)[index];
			if (!(row instanceof HTMLElement)) throw new Error(`no track row at index ${index}`);
			const target = document.querySelector(`section.rb-deck[data-deck="${deckId}"]`);
			if (!(target instanceof HTMLElement)) throw new Error(`no deck ${deckId}`);

			const start = new DataTransfer();
			row.dispatchEvent(
				new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: start })
			);

			const carried = carryPayload ? start : new DataTransfer();
			const over = new DragEvent('dragover', {
				bubbles: true,
				cancelable: true,
				dataTransfer: carried
			});
			target.dispatchEvent(over);
			const report = {
				dragOverAccepted: over.defaultPrevented,
				typesDuringDragOver: [...carried.types],
				payloadOnDrop: carried.getData(mime)
			};

			target.dispatchEvent(
				new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: carried })
			);
			// The real gesture always ends with a dragend, and the app removes its
			// drag ghost there. Skipping it would leave DOM litter behind.
			row.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: carried }));
			return report;
		},
		{
			rowSelector: TRACK_ROW,
			index: rowIndex,
			deckId: deck,
			carryPayload: options.carryPayload,
			mime: TRACK_STABLE_MIME
		}
	);
}

async function _dblClickLoad(page: Page, index: number): Promise<string> {
	const row = page.locator(TRACK_ROW).nth(index);
	const stableId = await row.getAttribute('data-stable-id');
	if (stableId === null) throw new Error(`row ${index} has no data-stable-id`);
	await row.locator('td.c-title').dblclick();
	return stableId;
}

async function _clickLoadOntoDeck(page: Page, index: number, deck: DeckId): Promise<string> {
	const row = page.locator(TRACK_ROW).nth(index);
	const stableId = await row.getAttribute('data-stable-id');
	if (stableId === null) throw new Error(`row ${index} has no data-stable-id`);
	await row.hover();
	await row.locator(`button[title="Load onto deck ${deck}"]`).click();
	return stableId;
}

/**
 * Wait until the deck's PRESENTED transport state matches `audible`.
 *
 * The bare Playwright timeout this used to raise reads as "the deck is
 * broken", and it cost a full investigation once already: the deck was loaded
 * and simply out of audio, because a 60s fixture had run dry mid-suite. The
 * position, the duration and the transport clock separate those two cases at a
 * glance, so they are stated rather than left for whoever opens the trace.
 */
async function _waitForAudible(page: Page, deck: DeckId, audible: boolean): Promise<void> {
	try {
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
	} catch {
		const state = (await _query(page)).decks[deck];
		throw new Error(
			`deck ${deck} never reached audible=${audible}: ` +
				`position ${state.position_ms.toFixed(0)}ms of ${state.duration_ms ?? 'unknown'}ms, ` +
				`playing=${state.playing}, audible=${state.audible}, ` +
				`transport_pending=${state.transport_pending}, ` +
				`presented=${state.transport_clock.presented_revision} ` +
				`desired=${state.transport_clock.desired_revision}, ` +
				`stable_id=${state.stable_id ?? 'null'}, ` +
				`command_error=${state.command_error ?? 'null'}`
		);
	}
}

/**
 * Drive one deck's transport through the IPC and wait for the audio graph to
 * have caught up, not merely for the promise to resolve.
 *
 * `playing` is the latching half of the wait. The presentation clock is the
 * half that earns the poll interval: it only advances inside the rAF tick, and
 * `quantizedSeek` REFUSES the paused-cursor branch while it lags (see
 * `_presentationPending` in audio-engine.svelte.ts), so a seek issued before
 * this settles is a different code path than the one under test.
 *
 * The IPC rather than the PLAY button on purpose, for the same reason `_seek`
 * gives below: two tests here own the play/pause button's behavior, and a
 * shared setup helper must not be driven through another test's subject.
 */
async function _setTransport(page: Page, deck: DeckId, playing: boolean): Promise<void> {
	await _dispatch(page, { type: 'play', deck, playing });
	await page.waitForFunction(
		({ deckId, expected }) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) return false;
			const state = ipc.query().decks[deckId];
			return (
				state.playing === expected &&
				!state.transport_pending &&
				state.transport_clock.presented_revision === state.transport_clock.desired_revision
			);
		},
		{ deckId: deck, expected: playing },
		{ timeout: 15_000, polling: TRANSPORT_POLL_MS }
	);
}

/**
 * Return one deck's playhead to `positionMs`, and wait for it to land.
 *
 * The UI's own way back is a CUE press, which also PAUSES and stamps a cue
 * point - both asserted in their own right below, so using it here would have
 * one test set up through another test's subject. This is the agent-native IPC
 * endpoint the house rules require every UI control to have.
 *
 * WHY THE DECK IS STOPPED FIRST (#689). This used to dispatch the seek at a
 * PLAYING deck and then wait for `position_ms` to be observed inside
 * [target, target + 2000). That is a window that CLOSES ON ITS OWN: playback
 * carries the playhead past it in two seconds, and once past, the predicate
 * can never become true again. Playwright polls on rAF by default, a loaded
 * runner throttles or suspends rAF, and a polling gap wider than the window is
 * all it takes - the wait then burns its whole 15s timeout and reports a seek
 * that in fact landed correctly. Measured at roughly a 36% failure rate on the
 * blocking gate, always with the tell that the playhead sat near the timeout's
 * own duration counted from zero.
 *
 * Stopping the deck removes the race rather than widening the window: the
 * landing position is then STATIC, so the predicate LATCHES and any later
 * observation satisfies it no matter how long the poller stalled. Transport is
 * restored as found, so a deck that was playing plays on from the new
 * position. Widening the tolerance was tried and rejected: the window's width
 * was never the variable, the poller's latency was.
 */
async function _seek(page: Page, deck: DeckId, positionMs: number): Promise<void> {
	const wasPlaying = (await _query(page)).decks[deck].playing;
	if (wasPlaying) await _setTransport(page, deck, false);
	await _dispatch(page, { type: 'seek', deck, position_ms: positionMs });
	try {
		await page.waitForFunction(
			({ deckId, target, tolerance }) => {
				const ipc = window.musicDjToolsPerformance;
				if (ipc === undefined) return false;
				const state = ipc.query().decks[deckId];
				return (
					!state.transport_pending && Math.abs(state.position_ms - target) < tolerance
				);
			},
			{ deckId: deck, target: positionMs, tolerance: SEEK_LANDING_TOLERANCE_MS },
			{ timeout: 15_000, polling: TRANSPORT_POLL_MS }
		);
	} catch {
		// Same reason `_waitForAudible` states its evidence: a bare timeout here
		// names neither the deck nor where the playhead actually went.
		const state = (await _query(page)).decks[deck];
		throw new Error(
			`deck ${deck} did not land a seek to ${positionMs}ms: ` +
				`position ${state.position_ms.toFixed(0)}ms of ${state.duration_ms ?? 'unknown'}ms, ` +
				`playing=${state.playing} (was ${wasPlaying} before the seek), ` +
				`audible=${state.audible}, ` +
				`transport_pending=${state.transport_pending}, ` +
				`stable_id=${state.stable_id ?? 'null'}, ` +
				`command_error=${state.command_error ?? 'null'}`
		);
	}
	// State as found: a deck that arrived playing leaves playing, from the new
	// position. Deliberately outside the try, so a failed landing surfaces its
	// own evidence rather than whatever the restore went on to do.
	if (wasPlaying) await _setTransport(page, deck, true);
}

/** Wait until one deck's playhead has traveled past `positionMs`.
 *
 * Latching rather than windowed, so a stalled poller cannot make this MISS the
 * way `_seek` used to - but the playhead is still moving on its own, and an
 * rAF gap here spends the 30s budget doing nothing. Same wall-clock poll. */
async function _playUntilPast(page: Page, deck: DeckId, positionMs: number): Promise<void> {
	await page.waitForFunction(
		({ deckId, past }) => {
			const ipc = window.musicDjToolsPerformance;
			return ipc !== undefined && ipc.query().decks[deckId].position_ms > past;
		},
		{ deckId: deck, past: positionMs },
		{ timeout: 30_000, polling: TRANSPORT_POLL_MS }
	);
}

/**
 * Playhead movement over one sample window, scaled to TRANSPORT_SAMPLE_MS.
 *
 * MEASURED interval, not an assumed one. This used to read the position from
 * node, wait TRANSPORT_SAMPLE_MS, and read it again - so the two Playwright
 * round trips landed INSIDE the window being attributed to the audio clock. On
 * a loaded machine those round trips are worth hundreds of ms each, and a
 * correctly playing deck reported 3201ms of travel for a 1500ms window: a real
 * measurement of the wrong interval. `_measureRate` below already avoids this
 * by timing itself in-page, and this now does the same, then reports what the
 * deck would have travelled in one honest window.
 */
async function _sampleAdvanceMs(page: Page, deck: DeckId): Promise<number> {
	const rate = await _measureRate(page, deck, TRANSPORT_SAMPLE_MS);
	return rate * TRANSPORT_SAMPLE_MS;
}

/** DeckHeader's MM:SS.d formatter, mirrored so the readout can be asserted. */
function _clockText(positionMs: number): string {
	const totalS = Math.max(0, positionMs) / 1000;
	const m = Math.floor(totalS / 60);
	const s = Math.floor(totalS % 60);
	const tenths = Math.floor((totalS * 10) % 10);
	return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${tenths}`;
}

// ----- deck-scoped locators --------------------------------------------------
/**
 * A control inside ONE deck panel.
 *
 * `section.rb-deck` is not decoration: `[data-deck]` matches EIGHT elements
 * (four `.rb-waverow` overview lanes and four `.rb-deck` panels), so a bare
 * `[data-deck="1"]` is ambiguous. `:visible` scopes the locator to the control
 * a person can interact with rather than hidden responsive UI.
 */
function _control(page: Page, deck: DeckId, control: string) {
	return page.locator(
		`section.rb-deck[data-deck="${deck}"] [data-performance-control="${control}"]:visible`
	);
}

/**
 * Pin the visible control count before clicking. A new duplicate must fail
 * loudly instead of being silently absorbed by `.first()`.
 */
const EXPECTED_VISIBLE_CONTROLS: Readonly<Record<string, number>> = {
	'master-tempo': 1
};

/** Click a control after asserting it resolved to the count we expect. */
async function _pressControl(page: Page, deck: DeckId, control: string): Promise<void> {
	const button = _control(page, deck, control);
	await expect(
		button,
		`deck ${deck} ${control} did not resolve to its expected visible count`
	).toHaveCount(EXPECTED_VISIBLE_CONTROLS[control] ?? 1);
	await button.first().click();
}

/** Set a two-state control to `on`/`off` through its real button. */
async function _setToggle(
	page: Page,
	deck: DeckId,
	control: string,
	state: 'on' | 'off'
): Promise<void> {
	const button = _control(page, deck, control).first();
	if ((await button.getAttribute('data-state')) === state) return;
	await _pressControl(page, deck, control);
	await expect(button).toHaveAttribute('data-state', state);
}

// ----- deck-scoped measurement -----------------------------------------------
/**
 * Playhead advance per wall-clock millisecond on ONE deck.
 *
 * Both readings are taken inside the page against the same clock, so the
 * measurement cannot pick up Playwright round-trip latency, and both are read
 * from `decks[deck]`, so the number is always attributable to that deck.
 */
/**
 * Both ends of the interval are read INSIDE an animation frame, because the
 * deck's reported `position_ms` only refreshes once per frame. Reading it at
 * two arbitrary instants measures a step function against a smooth clock, and
 * the error is the frame period - invisible at 60fps, but this suite also runs
 * headless, where WebKit was measured serving frames every ~557ms. There the
 * same 2s window aliased to 0.81x-1.18x on a deck that was playing at exactly
 * 1.0x: sampling across whole frames over the same run gave 2206ms of playhead
 * for 2205ms of wall clock, while a fresh AudioContext held 1.000x throughout.
 * Frame-aligning removes the skew without touching a single tolerance.
 */
async function _measureRate(page: Page, deck: DeckId, windowMs: number): Promise<number> {
	return page.evaluate(
		async ({ deckId, ms }) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const read = (): Promise<{ pos: number; t: number }> =>
				new Promise((resolve) =>
					requestAnimationFrame(() =>
						resolve({ pos: ipc.query().decks[deckId].position_ms, t: performance.now() })
					)
				);
			const first = await read();
			await new Promise((resolve) => setTimeout(resolve, ms));
			const second = await read();
			const wallMs = second.t - first.t;
			if (wallMs <= 0) throw new Error(`non-positive wall interval ${wallMs}ms`);
			return (second.pos - first.pos) / wallMs;
		},
		{ deckId: deck, ms: windowMs }
	);
}

/** One dominant-frequency reading from the deck's own analyser, in Hz. */
async function _captureDominantHz(page: Page, deck: DeckId): Promise<number> {
	return page.evaluate(
		({ deckId, band }) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) throw new Error('performance IPC is not installed');
			const snapshot = ipc.capture(deckId);
			const db = Array.from(snapshot.frequency_db as ArrayLike<number>);
			const hzPerBin = snapshot.sample_rate_hz / snapshot.fft_size;
			const lo = Math.ceil(band[0] / hzPerBin);
			const hi = Math.min(Math.floor(band[1] / hzPerBin), db.length - 2);
			let peakBin = -1;
			let peakDb = Number.NEGATIVE_INFINITY;
			for (let bin = Math.max(lo, 1); bin <= hi; bin++) {
				if (db[bin] > peakDb) {
					peakDb = db[bin];
					peakBin = bin;
				}
			}
			if (peakBin < 0) throw new Error('no analyser bin inside the measurement band');
			// Parabolic interpolation across the log-magnitude peak. Without it a
			// reading is quantised to the ~10.8 Hz bin width, which is coarse
			// enough to swamp a one-semitone assertion.
			const left = db[peakBin - 1];
			const centre = db[peakBin];
			const right = db[peakBin + 1];
			const denominator = left - 2 * centre + right;
			let offset = denominator === 0 ? 0 : (0.5 * (left - right)) / denominator;
			if (!Number.isFinite(offset) || Math.abs(offset) > 1) offset = 0;
			return (peakBin + offset) * hzPerBin;
		},
		{ deckId: deck, band: MEASUREMENT_BAND_HZ }
	);
}

/**
 * Wait until the deck's spectrum has REACHED steady state, then return.
 *
 * A tempo or key change does not reach the analyser instantly: the stretch
 * processor is already several blocks ahead, and the AnalyserNode sits after
 * it, so the first capture after a command can still show the OLD pitch. That
 * is a transition, not a disagreement, and measuring across it was worth a
 * 320Hz spread on a correct app.
 *
 * This is a settle phase, NOT a retry that hides a bad reading: nothing
 * measured here is used as a result, the measurement window afterwards still
 * keeps every sample it takes, and a spectrum that never settles fails here
 * with its whole history attached.
 */
async function _waitForSpectrumSettled(page: Page, deck: DeckId): Promise<void> {
	const seen: number[] = [];
	const deadline = Date.now() + SPECTRUM_SETTLE_TIMEOUT_MS;
	while (Date.now() < deadline) {
		seen.push(await _captureDominantHz(page, deck));
		const recent = seen.slice(-2);
		if (recent.length === 2 && Math.abs(recent[0] - recent[1]) < SPECTRUM_SPREAD_TOLERANCE_HZ) {
			return;
		}
		await page.waitForTimeout(80);
	}
	throw new Error(
		`deck ${deck} spectrum never settled within ${SPECTRUM_SETTLE_TIMEOUT_MS}ms: ` +
			seen.map((hz) => hz.toFixed(1)).join(', ')
	);
}

/**
 * Dominant frequency of ONE deck, as the median of SPECTRUM_SAMPLES captures.
 *
 * Every sample is kept and the SPREAD is asserted: a disagreeing capture fails
 * the measurement loudly instead of being quietly dropped, which is the failure
 * mode that made an earlier harness report a pass it had not earned.
 */
async function _dominantHz(page: Page, deck: DeckId): Promise<number> {
	await _waitForSpectrumSettled(page, deck);
	const samples: number[] = [];
	for (let i = 0; i < SPECTRUM_SAMPLES; i++) {
		samples.push(await _captureDominantHz(page, deck));
		await page.waitForTimeout(60);
	}
	const spread = Math.max(...samples) - Math.min(...samples);
	expect(
		spread,
		`deck ${deck} analyser readings disagree by ${spread.toFixed(1)}Hz: ${samples
			.map((hz) => hz.toFixed(1))
			.join(', ')}`
	).toBeLessThan(SPECTRUM_SPREAD_TOLERANCE_HZ);
	return [...samples].sort((a, b) => a - b)[Math.floor(samples.length / 2)];
}

/** Assert `actual` is `expected` within FREQUENCY_RATIO_TOLERANCE, relatively. */
function _expectHzNear(actual: number, expected: number, what: string): void {
	const drift = Math.abs(actual - expected) / expected;
	expect(
		drift,
		`${what}: expected ~${expected.toFixed(1)}Hz, measured ${actual.toFixed(1)}Hz`
	).toBeLessThan(FREQUENCY_RATIO_TOLERANCE);
}

// ----- transport helpers -----------------------------------------------------
async function _ensurePlaying(page: Page, deck: DeckId, playing: boolean): Promise<void> {
	const state = await _query(page);
	if (state.decks[deck].playing !== playing) {
		await _control(page, deck, 'play').click();
	}
	await _waitForAudible(page, deck, playing);
}

/**
 * Put the pitch fader of one deck back to exactly 0% with a real pointer click.
 *
 * The keyboard has no "centre" key (Home is -range, End is +range).
 *
 * The offset is MEASURED, never a constant. The component maps a click to
 * `1 - (clientY - rect.top - THUMB_H / 2) / (trackH - THUMB_H)`, which is 0.5 -
 * ratio 1.0 exactly - at `trackH / 2`, whatever the rendered height is. This
 * used to hardcode 48px for a 96px track, and pin ebb1def0234c (7374bbaec,
 * Thu 3 Sep 2026) then made the track `height: 100%` with a 64px floor because
 * the fixed 96 was drawing 0% above centre. At the ~64px this layout actually
 * gives the fader, a 48px click is -9.8%, and the wait below then sat out its
 * whole timeout against a fader that had done exactly what it was told.
 */
async function _centrePitch(page: Page, deck: DeckId): Promise<void> {
	const fader = _control(page, deck, 'pitch');
	const box = await fader.boundingBox();
	if (box === null) throw new Error(`deck ${deck} pitch fader has no bounding box`);
	await fader.click({ position: { x: 3, y: box.height / 2 } });
	await page.waitForFunction(
		(deckId) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) return false;
			const state = ipc.query().decks[deckId];
			return Math.abs(state.pitch - 1) < 1e-6 && !state.transport_pending;
		},
		deck,
		{ timeout: 15_000 }
	);
}

/** Select a pitch range through its real button. The engine REFUSES a ratio
 * outside the selected range, so this is part of the tempo control flow. */
async function _selectPitchRange(page: Page, deck: DeckId, pct: number): Promise<void> {
	const button = page.locator(
		`section.rb-deck[data-deck="${deck}"] ` +
			`[data-performance-control="pitch-range"][data-range="${pct}"]:visible`
	);
	await expect(button).toHaveCount(1);
	await button.click();
	await expect(button).toHaveAttribute('data-state', 'on');
}

/** Drive the pitch fader with a real key press and wait for the engine. */
async function _pitchKey(page: Page, deck: DeckId, key: string, ratio: number): Promise<void> {
	const fader = _control(page, deck, 'pitch');
	await fader.focus();
	await fader.press(key);
	await page.waitForFunction(
		({ deckId, expected }) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) return false;
			const state = ipc.query().decks[deckId];
			return Math.abs(state.pitch - expected) < 1e-6 && !state.transport_pending;
		},
		{ deckId: deck, expected: ratio },
		{ timeout: 15_000 }
	);
}

/**
 * Wait until the deck has SETTLED on the incoming track (#774).
 *
 * `stable_id !== null` alone is not that wait. On a RE-load the OUTGOING track
 * still satisfies it - and when the same row is loaded twice, even the id is
 * identical - so the wait returned mid-swap, with `transport_pending` still
 * true. Sampling deck 1 across a re-load showed the deck then passing through a
 * ~370ms window reporting NOTHING loaded (stable_id null, duration_ms null,
 * position 0) before the incoming track committed and auto-played. Whichever
 * point of that swap the next test happened to sample decided its failure, so
 * one race produced three unrelated-looking signatures.
 *
 * `transport_pending` is false inside the blank window too, so a single sample
 * of "loaded and not pending" is not enough either. Requiring it to HOLD across
 * consecutive polls is what distinguishes a settled deck from a deck caught
 * mid-swap. A load onto an empty deck settles on the first poll and pays
 * nothing for this.
 */
async function _waitForDeckLoaded(page: Page, deck: DeckId): Promise<void> {
	const deadline = Date.now() + DECK_SETTLE_TIMEOUT_MS;
	let settled = 0;
	for (;;) {
		const state = (await _query(page)).decks[deck];
		settled = state.stable_id !== null && !state.transport_pending ? settled + 1 : 0;
		if (settled >= DECK_SETTLE_POLLS) return;
		if (Date.now() > deadline) {
			// Every deck, not just the one waited on: nightly run 34187231703 (Tue 8
			// Sep 2026) waited on deck 1 while the app's own picker had landed the
			// track on deck 2, and this message could not say why deck 1 was passed
			// over. The picker's inputs (is_master, playing, stable_id) are stated
			// so the next failure names the exclusion instead of the symptom.
			const board = (await _query(page)).decks;
			const roster = (Object.keys(board) as unknown as DeckId[])
				.map((id) => {
					const d = board[id];
					return (
						`deck${id}[sid=${d.stable_id?.slice(0, 8) ?? 'null'} ` +
						`playing=${d.playing} audible=${d.audible} master=${d.is_master} ` +
						`pending=${d.transport_pending}]`
					);
				})
				.join(' ');
			throw new Error(
				`deck ${deck} never settled on a loaded track: ` +
					`stable_id=${state.stable_id ?? 'null'}, ` +
					`transport_pending=${state.transport_pending}, ` +
					`duration_ms=${state.duration_ms ?? 'null'}; board: ${roster}`
			);
		}
		await page.waitForTimeout(DECK_SETTLE_POLL_MS);
	}
}

/**
 * Wait for a deck that ALREADY HELD A TRACK to finish loading the next one.
 *
 * `_waitForDeckLoaded` cannot express this. It waits for `stable_id !== null`,
 * which on a reload is already true from the PREVIOUS load, so it returns
 * before the new load has even started; the caller then reads the old track's
 * telemetry, or reads inside the gap and sees nulls.
 *
 * The outgoing blank state is a real reload edge, but it can be shorter than
 * this suite's 100ms transport poll. `load_generation` increments only after
 * the real audio transaction commits its incoming track and load telemetry, so
 * it makes that otherwise transient edge durably observable without hammering
 * WebKit at a 1ms cadence.
 *
 * It has to be a signal the REPLACEMENT PATH cannot roll back, not merely one
 * that outlives a poll. A destructive replace ejects before it loads, and
 * engine.unload used to hand the slot a fresh empty state - generation back to
 * 0 - so a deck on generation 1 went 1 -> 0 -> 1 and this predicate could
 * never become true. engine.unload now carries the count forward for exactly
 * this reason; see its comment.
 */
async function _waitForDeckReloaded(
	page: Page,
	deck: DeckId,
	previousLoadGeneration: number
): Promise<void> {
	await page.waitForFunction(
		({ deckId, previousLoadGeneration }) => {
			const state = window.musicDjToolsPerformance?.query().decks[deckId];
			return (
				state !== undefined &&
				state.load_generation > previousLoadGeneration &&
				state.stable_id !== null &&
				state.last_load_stages !== null
			);
		},
		{ deckId: deck, previousLoadGeneration },
		{ timeout: 45_000, polling: TRANSPORT_POLL_MS }
	);
}

// Sequential execution comes from workers: 1 + fullyParallel: false in the
// webkit-deckload config, required because tests share the beforeAll page.
// mode: serial is forbidden here: the first failure would skip the quarantined
// remainder (#712).

test.describe('webkit performance controls on the engine-served build', () => {
	let page: Page;

	test.beforeAll(async ({ browser }) => {
		page = await browser.newPage();
		page.on('console', _onConsole);
		page.on('pageerror', (error) => pageErrors.push(String(error)));
		// The app's REAL persistence, not a stub: the remembered choice it would
		// have written itself, on disk AND in localStorage, so the double-click
		// load takes the deterministic no-confirm path. Disk is authoritative for
		// confirm keys since PR #4014 (hydrateConfirmFromDisk drops a key the disk
		// map lacks), so a localStorage-only seed is reset to "ask" by the boot
		// GET. The perf ring is cleared so a stale entry from an earlier run
		// cannot be mistaken for this run's telemetry.
		const put = await page.request.put('/api/v1/ui-prefs', {
			data: { confirm: { dblclick_load_play: false } }
		});
		expect(put.ok(), `seed confirm.dblclick_load_play: HTTP ${put.status()}`).toBe(true);
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
		await page.request.put('/api/v1/ui-prefs', {
			data: { confirm: { dblclick_load_play: null } }
		});
		await page.close();
	});

	/**
	 * Give every test the full 60s of fixture audio (see PLAYHEAD RUNWAY above).
	 *
	 * Deck 1 only, deliberately. Deck 2 is loaded and left playing by the load
	 * test and does run dry later, which trips auto-play into logging a real
	 * ui-error about a successor this two-track fixture never had - but nothing
	 * below reads deck 2's position again, so that line is noise rather than a
	 * failure. Rewinding deck 2 as well would silence it and is worth doing, but
	 * only behind a run that can actually verify it: these are real-time audio
	 * assertions, and a loaded machine fails them for reasons of its own.
	 *
	 * Skipped before the load test, which is the one that puts a track there.
	 */
	test.beforeEach(async () => {
		if ((await _query(page)).decks[1].stable_id === null) return;
		await _seek(page, 1, 0);
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

	test('the deck critical path carries no stem work, and settles it afterwards', async () => {
		// LAZY-STEMS, in WebKit against the production build. the maintainer, in-app
		// feedback 2026-08-31T14:50:06Z: "stems and other secondary items should
		// be lazy loaded". This is the behavioural half of
		// tests/unit/deck-lazy-stems.test.mjs (which is structural).
		//
		// The fixture library carries no stem bundles, so the settled answer here
		// is `unavailable`. That is the point: the deck must reach a PLAYABLE
		// state without ever having asked, and only then find out.
		// Deck 1 already holds this track from the previous test, so this is a
		// RELOAD. Capture the durable generation before dispatching it.
		//
		// The gesture is a DROP onto CH1, not a double-click. Double-click does
		// not choose its deck: pickDoubleClickDeck ranks empty slots first and
		// excludes the master, and by this point CH1 is the loaded master while
		// CH3/CH4 are still empty - so a double-click here loads CH3 and the
		// wait on CH1 below would sit out its full timeout while nothing was
		// ever wrong. A drop is the app's only gesture that names its target
		// deck AND is allowed to replace the live master (Deck.svelte's
		// onTrackDrop unloads without refuseIfMaster; the "Load onto deck N"
		// button sets it and would refuse). It is the same replace a DJ does
		// when they drag a track onto a playing deck.
		const reloadedId = (await _query(page)).decks[1].stable_id;
		// Click first, as a user does, so the drag carries this row alone: a
		// drag from a row inside a multi-row selection carries every selected id.
		await page.locator(TRACK_ROW).first().click();
		const previousLoadGeneration = (await _query(page)).decks[1].load_generation;
		const reload = await _dragRowToDeck(page, 0, 1, { carryPayload: true });
		expect(reload.dragOverAccepted, 'CH1 refused the reload drag').toBe(true);
		expect(reload.payloadOnDrop, 'the reload drag carried the wrong track').toBe(reloadedId);
		await _waitForDeckReloaded(page, 1, previousLoadGeneration);

		const state = await _query(page);
		const stages = state.decks[1].last_load_stages;
		expect(stages, 'deck 1 reported no load stages').not.toBeNull();
		// Each of these is a stage a DJ used to wait through before the deck
		// could play. Any one reappearing is the regression.
		for (const deferred of [
			'probeStem',
			'fetchStems',
			'decodeStems',
			'stemProcessorCreate'
		]) {
			expect(
				stages?.[deferred],
				`${deferred} is back in the deck-load critical path`
			).toBeUndefined();
		}
		// The mix path really did run: the deck is playable, not merely fast.
		expect(stages?.decodeMix).toBeGreaterThanOrEqual(0);
		expect(stages?.stretchCreate).toBeGreaterThanOrEqual(0);
		expect(state.decks[1].processor_error).toBeNull();

		// The secondary load settles off the critical path rather than hanging
		// in `loading` for ever.
		await page.waitForFunction(
			() => window.musicDjToolsPerformance?.query().decks[1].stems.status !== 'loading',
			undefined,
			{ timeout: 30_000 }
		);
		const settled = await _query(page);
		expect(['unavailable', 'ready', 'error']).toContain(settled.decks[1].stems.status);

		// AGENT-NATIVE PARITY (house rule): the bottom-left badge and the IPC
		// must never disagree about the same fact. A settled non-error deck
		// shows no badge; whenever a badge IS shown its data-stem-load must be
		// exactly the status the IPC reports.
		const badge = page.locator(
			'section.rb-deck[data-deck="1"] [data-testid="deck-secondary-load"]'
		);
		if (settled.decks[1].stems.status === 'error') {
			await expect(badge).toHaveAttribute('data-stem-load', 'error');
		} else {
			await expect(badge).toHaveCount(0);
		}

		// A drop is a load, not a load+PLAY, so this test leaves CH1 stopped
		// where the double-click it replaced left it running. Hand the deck
		// back playing: the transport tests below inherit this deck and read
		// its audio clock, and a stopped deck would read to them as broken
		// rather than as never started. Stated here, at the test that changed
		// it, rather than left for whoever debugs the next failure.
		await _ensurePlaying(page, 1, true);
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
		// Deck 1 arrives here already running the real audio clock, left that
		// way by the reload test above (which re-plays it after its drop, the
		// drop being a load with no PLAY). That inherited running state is what
		// is under test, not a setup step this test performs.
		await _waitForAudible(page, 1, true);
		const advancedFromLoad = await _sampleAdvanceMs(page, 1);
		expect(advancedFromLoad).toBeGreaterThan(TRANSPORT_SAMPLE_MS * 0.5);
		expect(advancedFromLoad).toBeLessThan(TRANSPORT_SAMPLE_MS * 1.5);

		const playButton = _control(page, 1, 'play');
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

	test('a tempo change drives the playhead rate, not just the fader', async () => {
		await _ensurePlaying(page, 1, true);
		await _centrePitch(page, 1);

		const baseRate = await _measureRate(page, 1, RATE_WINDOW_MS);
		expect(baseRate, `deck 1 plays at ${baseRate.toFixed(4)}x at 0% pitch`).toBeGreaterThan(0.96);
		expect(baseRate).toBeLessThan(1.04);

		await _selectPitchRange(page, 1, PITCH_RANGE_PCT);
		await _pitchKey(page, 1, 'End', PITCH_MAX_RATIO);
		await _waitForAudible(page, 1, true);

		const fastRate = await _measureRate(page, 1, RATE_WINDOW_MS);
		const applied = fastRate / baseRate;
		expect(
			applied,
			`playhead rate went ${baseRate.toFixed(4)}x -> ${fastRate.toFixed(4)}x, ` +
				`a factor of ${applied.toFixed(4)} for a +${PITCH_RANGE_PCT}% fader move`
		).toBeGreaterThan(PITCH_MAX_RATIO - 0.03);
		expect(applied).toBeLessThan(PITCH_MAX_RATIO + 0.03);

		// The rendered readout must agree with the audio path it produced.
		await expect(_control(page, 1, 'pitch')).toHaveAttribute(
			'aria-valuenow',
			String(PITCH_RANGE_PCT)
		);

		await _centrePitch(page, 1);
	});

	test('MASTER TEMPO holds the key under a tempo change, and bypass shifts it', async () => {
		await _ensurePlaying(page, 1, true);
		await _centrePitch(page, 1);
		await _setToggle(page, 1, 'master-tempo', 'on');
		await _waitForAudible(page, 1, true);

		// The fixture's landmark tone, decoded and played at 1.0.
		const baseHz = await _dominantHz(page, 1);
		expect(baseHz, `deck 1 landmark tone measured ${baseHz.toFixed(1)}Hz`).toBeGreaterThan(1_950);
		expect(baseHz).toBeLessThan(2_050);

		await _selectPitchRange(page, 1, PITCH_RANGE_PCT);
		await _pitchKey(page, 1, 'End', PITCH_MAX_RATIO);
		await _waitForAudible(page, 1, true);

		// Prove the tempo really applied FIRST. Without this, "the key held"
		// would pass just as happily on a tempo control that did nothing at all.
		const fastRate = await _measureRate(page, 1, RATE_WINDOW_MS);
		expect(
			fastRate,
			`tempo did not reach the audio path: rate is ${fastRate.toFixed(4)}x`
		).toBeGreaterThan(PITCH_MAX_RATIO - 0.04);

		const heldHz = await _dominantHz(page, 1);
		_expectHzNear(heldHz, baseHz, `MASTER TEMPO on must hold the key at +${PITCH_RANGE_PCT}%`);

		// Bypass it: composeStretchSemitones now contributes 12*log2(ratio)
		// semitones, so the same tempo has to drag the tone up with it.
		await _setToggle(page, 1, 'master-tempo', 'off');
		await _waitForAudible(page, 1, true);
		const shiftedHz = await _dominantHz(page, 1);
		_expectHzNear(
			shiftedHz,
			baseHz * PITCH_MAX_RATIO,
			`MASTER TEMPO off must shift the key with tempo`
		);

		await _setToggle(page, 1, 'master-tempo', 'on');
		await _centrePitch(page, 1);
		await _waitForAudible(page, 1, true);
	});

	test('a key nudge shifts the audio one semitone without touching tempo', async () => {
		await _ensurePlaying(page, 1, true);
		await _centrePitch(page, 1);
		await _setToggle(page, 1, 'master-tempo', 'on');
		await _waitForAudible(page, 1, true);

		const baseHz = await _dominantHz(page, 1);

		await _pressControl(page, 1, 'key-nudge-up');
		await _waitForAudible(page, 1, true);
		const upHz = await _dominantHz(page, 1);
		_expectHzNear(upHz, baseHz * SEMITONE_RATIO, 'one semitone up');

		await _pressControl(page, 1, 'key-nudge-down');
		await _pressControl(page, 1, 'key-nudge-down');
		await _waitForAudible(page, 1, true);
		const downHz = await _dominantHz(page, 1);
		_expectHzNear(downHz, baseHz / SEMITONE_RATIO, 'one semitone down');

		// Key shift composes into the stretcher's semitone field and must NEVER
		// reach transport rate. A whole semitone moved; the playhead must not.
		const rate = await _measureRate(page, 1, RATE_WINDOW_MS);
		expect(
			rate,
			`key shift leaked into transport: playhead runs at ${rate.toFixed(4)}x`
		).toBeGreaterThan(0.96);
		expect(rate).toBeLessThan(1.04);

		await _pressControl(page, 1, 'key-nudge-up');
		await _waitForAudible(page, 1, true);
		expect((await _query(page)).decks[1].key_shift_semitones).toBe(0);
	});

	test('CUE stamps a cue point and returns the playhead to it', async () => {
		await _ensurePlaying(page, 1, true);
		await _centrePitch(page, 1);

		// Travel a deliberate distance first. This used to be whatever the tests
		// before it happened to leave on the playhead - the same accidental
		// coupling that let the fixture run dry mid-suite - and the cue point it
		// produces is asserted below, so it cannot be an accident.
		await _playUntilPast(page, 1, CUE_RUNWAY_MS);

		// Paused with no cue set, the engine stamps one at the current position.
		await _ensurePlaying(page, 1, false);
		const pausedAt = (await _query(page)).decks[1].position_ms;
		expect(pausedAt, 'the cue point must be non-zero to prove a real return').toBeGreaterThan(
			1_000
		);

		await _pressControl(page, 1, 'cue');
		await page.waitForFunction(
			(deckId: DeckId) => window.musicDjToolsPerformance?.query().decks[deckId].cue_ms !== null,
			1 as DeckId,
			{ timeout: 15_000 }
		);
		const cueMs = (await _query(page)).decks[1].cue_ms;
		expect(cueMs).not.toBeNull();
		expect(Math.abs((cueMs ?? 0) - pausedAt)).toBeLessThan(50);

		// Play well past it, then press CUE: the contract is return AND pause.
		await _ensurePlaying(page, 1, true);
		await _playUntilPast(page, 1, (cueMs ?? 0) + 2_000);

		await _pressControl(page, 1, 'cue');
		await _waitForAudible(page, 1, false);
		const after = await _query(page);
		expect(after.decks[1].playing).toBe(false);
		expect(
			Math.abs(after.decks[1].position_ms - (cueMs ?? 0)),
			`CUE left the playhead at ${after.decks[1].position_ms.toFixed(0)}ms, ` +
				`cue point is ${(cueMs ?? 0).toFixed(0)}ms`
		).toBeLessThan(150);

		// The readout a DJ looks at must agree with where the audio actually is.
		await expect(page.locator('section.rb-deck[data-deck="1"] .elapsed')).toHaveText(
			_clockText(after.decks[1].position_ms)
		);
	});

	test('an engaged loop keeps the playhead inside its window', async () => {
		// The UI's loop control is a BEAT loop and the fixture has no beatgrid
		// (see the file header), so the millisecond loop is driven through the
		// agent-native IPC endpoint every UI control is required to have. The
		// button's own inert contract is asserted by the next test.
		await _ensurePlaying(page, 1, true);
		await _centrePitch(page, 1);

		const startMs = (await _query(page)).decks[1].position_ms;
		const inMs = startMs + 400;
		const outMs = inMs + LOOP_LENGTH_MS;
		await _dispatch(page, { type: 'loop', deck: 1, loop: { in_ms: inMs, out_ms: outMs } });
		await _waitForAudible(page, 1, true);

		const samples = await page.evaluate(
			async ({ deckId, forMs, everyMs }) => {
				const ipc = window.musicDjToolsPerformance;
				if (ipc === undefined) throw new Error('performance IPC is not installed');
				const readings: Array<{ observedAtMs: number; positionMs: number }> = [];
				const until = performance.now() + forMs;
				while (performance.now() < until) {
					readings.push({
						observedAtMs: performance.now(),
						positionMs: ipc.query().decks[deckId].position_ms
					});
					await new Promise((resolve) => setTimeout(resolve, everyMs));
				}
				return readings;
			},
			{ deckId: 1 as DeckId, forMs: LOOP_OBSERVE_MS, everyMs: LOOP_SAMPLE_INTERVAL_MS }
		);

		expect(samples.length).toBeGreaterThan(20);
		const firstSample = samples[0];
		const lastSample = samples.at(-1);
		if (firstSample === undefined || lastSample === undefined) {
			throw new Error('loop observation returned no samples');
		}
		expect(
			lastSample.observedAtMs - firstSample.observedAtMs,
			'loop observation must cover nearly its full wall-clock window'
		).toBeGreaterThanOrEqual(LOOP_OBSERVE_MS - LOOP_SAMPLE_INTERVAL_MS);
		// EVERY sample must sit inside the window. Not "most": one escape is the
		// whole defect, and averaging would hide exactly the bug worth catching.
		for (const { positionMs } of samples) {
			expect(
				positionMs,
				`playhead escaped the loop: ${positionMs.toFixed(0)}ms is outside ` +
					`${inMs.toFixed(0)}..${outMs.toFixed(0)}ms (all samples: ${samples
						.map((sample) => sample.positionMs.toFixed(0))
						.join(', ')})`
			).toBeGreaterThan(inMs - LOOP_EDGE_TOLERANCE_MS);
			expect(positionMs).toBeLessThan(outMs + LOOP_EDGE_TOLERANCE_MS);
		}

		// Bounded is only half the contract: a deck parked at loop-out, or one
		// whose presentation froze, is inside the window too. So every second of
		// the observation must also show wrap-aware advance, with the slices
		// anchored at the END so a tail stall cannot hide in a dropped remainder.
		// Nothing here counts wraps or distinct phases - `position_ms` is
		// published on requestAnimationFrame rather than at each audio loop
		// boundary, so both alias with the loop period. Bounded plus still
		// advancing for 5s over a 1.2s window is only possible by looping, which
		// is the DJ-visible property worth asserting. The transport unit
		// regression covers the multi-span projection underneath it.
		const stalledSlices = findStalledLoopSlices(samples, {
			sliceMs: LOOP_CONTINUITY_SLICE_MS,
			loopLengthMs: LOOP_LENGTH_MS,
			minimumAdvanceMs: LOOP_CONTINUITY_MIN_ADVANCE_MS
		});
		expect(
			describeStalledLoopSlices(stalledSlices),
			`the loop playhead stopped advancing during the ${LOOP_OBSERVE_MS}ms observation ` +
				`(all samples: ${samples.map((sample) => sample.positionMs.toFixed(0)).join(', ')})`
		).toBe('');

		await _dispatch(page, { type: 'loop', deck: 1, loop: null });
		await _waitForAudible(page, 1, true);
		expect((await _query(page)).decks[1].loop).toBeNull();
	});

	test('the beat-loop button is honestly inert without a beatgrid', async () => {
		// House rule: a control with no real data source renders inert and says
		// why. The fixture has no rekordbox analysis, so this is the contract the
		// button actually has here, and asserting it is what keeps the suite from
		// pretending a beat loop was tested.
		const loop = _control(page, 1, 'loop');
		await expect(loop).toBeDisabled();
		await expect(loop).toHaveAttribute('title', 'track has no beatgrid - beat loop unavailable');
	});

	test('a gridless deck plays and pauses, with BEAT SYNC and Q inert', async () => {
		// THE GRIDLESS TRANSPORT CONTRACT, specs/design_decision_09.md. The
		// engine genuinely refuses beat sync, quantized cue and beat loops
		// without a grid. The defect was that the refusal leaked into TRANSPORT,
		// so an unanalysed track could not be played at all - and an unanalysed
		// track is precisely what a DJ has just dragged in.
		//
		// This suite's whole library is gridless (no rekordbox vendor mapping,
		// so /anlz serves the empty-but-valid payload), which is why the case
		// the decision record asked for needs no fixture of its own: this deck
		// IS the case. Two halves, both load bearing. Play and pause must be
		// untouched by the missing grid, and the two controls that really do
		// need one must SAY so rather than accept a press and then fail.
		await _ensurePlaying(page, 1, true);
		expect((await _query(page)).decks[1].playing).toBe(true);

		await _control(page, 1, 'play').click();
		await _waitForAudible(page, 1, false);

		const paused = await _query(page);
		expect(paused.decks[1].playing).toBe(false);
		// Transport that works but surfaces "beat grid must contain at least 2
		// beats" on the way is still the reported defect, so the absence of a
		// refusal is asserted rather than inferred from the deck having moved.
		expect(paused.decks[1].command_error).toBeNull();

		// Honestly inert, not lit-but-dead: disabled AND self-describing, which
		// is the house contract for a control with no real data source.
		for (const control of ['beat-sync', 'quantize'] as const) {
			const button = _control(page, 1, control);
			await expect(button, `${control} must be disabled on a gridless deck`).toBeDisabled();
			await expect(button).toHaveAttribute('data-state', 'inert');
		}
	});

	test('a library row dragged onto a deck loads it', async () => {
		const row = page.locator(TRACK_ROW).first();
		const stableId = await row.getAttribute('data-stable-id');
		if (stableId === null) throw new Error('row 0 has no data-stable-id');
		// Click first, as a user does: a drag started from a row that is part of a
		// multi-row selection carries EVERY selected id, and this test asserts one.
		await row.click();

		const gesture = await _dragRowToDeck(page, 0, 3, { carryPayload: true });

		// The dragover must have been ACCEPTED: an unaccepted dragover means the
		// element never became a drop target and ondrop would never fire in a
		// real gesture, however the drop event behaves when dispatched by hand.
		expect(gesture.dragOverAccepted, 'deck 3 refused the dragover').toBe(true);
		expect(gesture.payloadOnDrop).toBe(stableId);

		await _waitForDeckLoaded(page, 3);
		expect((await _query(page)).decks[3].stable_id).toBe(stableId);
		await expect(page.locator('section.rb-deck[data-deck="3"] .title')).not.toHaveText(
			'No track loaded'
		);
	});

	test('a drop whose transfer carries nothing still loads the deck', async () => {
		// THE WEBKIT CASE. WKWebView hides custom MIME types during dragover and
		// its protected drag mode can hand back an empty getData, so a target that
		// gates on dataTransfer.types never calls preventDefault, never becomes a
		// drop target, and ondrop never fires: dragging a track onto a deck does
		// nothing at all in the packaged app while every chromium test passes.
		// Here the drag is begun through the row's own dragstart handler, then the
		// deck is offered a transfer that carries NOTHING.
		const row = page.locator(TRACK_ROW).first();
		const stableId = await row.getAttribute('data-stable-id');
		if (stableId === null) throw new Error('row 0 has no data-stable-id');
		await row.click();

		const gesture = await _dragRowToDeck(page, 0, 4, { carryPayload: false });

		expect(
			gesture.typesDuringDragOver,
			'this variant is only meaningful with an EMPTY transfer'
		).toHaveLength(0);
		expect(gesture.payloadOnDrop).toBe('');
		expect(
			gesture.dragOverAccepted,
			'deck 4 refused a dragover whose types were empty - this is the exact ' +
				'WKWebView defect: acceptance must come from the in-app drag state'
		).toBe(true);

		await _waitForDeckLoaded(page, 4);
		expect((await _query(page)).decks[4].stable_id).toBe(stableId);
		await expect(page.locator('section.rb-deck[data-deck="4"] .title')).not.toHaveText(
			'No track loaded'
		);
	});

	test('no uncaught page errors were raised during the run', () => {
		expect(pageErrors, pageErrors.join('\n')).toHaveLength(0);
	});
});
