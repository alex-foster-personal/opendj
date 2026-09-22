import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { engineBlockAfter } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Transport is NEVER gated by a grid-dependent feature.
 *
 * quantize_enabled and beat_sync_enabled both default to true (rekordbox
 * parity for an analysed library). Before this suite existed those defaults
 * reached requireBeatGrid from inside play(), pause(), pressCue(),
 * quantizedSeek() and setLoop(), so the first track a new user imported -
 * unanalysed, no PQTZ grid - could not be started, and once started could not
 * be STOPPED: pause() threw "pause cue: deck N requires a valid real PQTZ beat
 * grid" while the button still read pause and the refusal only ever reached
 * command_error. Silent and first-run hostile.
 *
 * The decided behaviour: play / pause / cue always run. On a deck whose track
 * has no real grid, quantize and beat sync simply have no effect, and their
 * controls say so on hover instead of failing on click. Grid-dependent
 * operations that are NOT transport (beat loops, engaging sync) keep their
 * requirement.
 *
 * Regression lines:
 * - if play, pause, pressCue, quantizedSeek or setLoop names requireBeatGrid
 *   again then an unanalysed deck cannot start, or cannot stop
 * - if effectiveQuantize / effectiveBeatSync stop folding in the grid check
 *   then a lit flag reaches grid math that throws
 * - if the Q or BEAT SYNC button loses its gridless disabled + tip then a lit
 *   control silently does nothing
 * - if setQuantize / setBeatSync stop announcing the inert state then the
 *   IPC and CLI paths engage a no-op in silence
 * - if engageBeatLoop or _synchronizeFollowers drops requireBeatGrid then a
 *   genuinely grid-dependent operation runs on no grid
 * - if a gridded deck stops snapping then rekordbox parity regressed
 */

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const JOG_DIAL_SRC = readFileSync(`${SRC}/lib/components/rb/deck/JogDial.svelte`, 'utf8');
const DECK_HEADER_SRC = readFileSync(`${SRC}/lib/components/rb/deck/DeckHeader.svelte`, 'utf8');
const PARITY_TODO_TITLE = 'not implemented - see PARITY-TODO';

/** Four consecutive real PQTZ beats at 127 BPM (0.472s apart). */
const REAL_PQTZ_BEATS = [
	{ n: 1, bpm: 127, t: 0.135 },
	{ n: 2, bpm: 127, t: 0.608 },
	{ n: 3, bpm: 127, t: 1.08 },
	{ n: 4, bpm: 127, t: 1.553 }
];

let grid;
let engine;

function _deck(overrides = {}) {
	return {
		deck_id: 1,
		stable_id: 'sid-under-test',
		quantize_enabled: true,
		beat_sync_enabled: true,
		anlz: null,
		...overrides
	};
}

function _withGrid(beats = REAL_PQTZ_BEATS) {
	return { beatgrid: { source: 'rekordbox', beats, status: 'ok' } };
}

before(async () => {
	grid = await loadTypeScriptModule('src/lib/player/grid-features.ts');
	engine = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts', {
		viteApiBase: 'https://gridless-transport.example.test'
	});
});

//-----------------------------------------------------------------------------
// (a) a gridless deck: the flags stay ON and simply stop reaching grid math
//-----------------------------------------------------------------------------

test('a loaded track with no grid makes quantize and beat sync ineffective, flags untouched', () => {
	const deck = _deck();
	assert.equal(deck.quantize_enabled, true, 'the default must remain ON');
	assert.equal(deck.beat_sync_enabled, true, 'the default must remain ON');
	assert.equal(grid.deckHasRealBeatGrid(deck), false);
	assert.equal(
		grid.effectiveQuantize(deck),
		false,
		'if quantize stays effective without a grid then pause() reaches grid math and refuses'
	);
	assert.equal(
		grid.effectiveBeatSync(deck),
		false,
		'if beat sync stays effective without a grid then play() reaches grid math and refuses'
	);
	assert.equal(grid.gridFeaturesInert(deck), true);
});

test('every shape of unusable grid counts as no grid, none of them throw', () => {
	for (const [label, anlz] of [
		['no anlz at all', null],
		['an empty grid', _withGrid([])],
		['a single beat', _withGrid([REAL_PQTZ_BEATS[0]])],
		['an out-of-order grid', _withGrid([REAL_PQTZ_BEATS[1], REAL_PQTZ_BEATS[0]])],
		['a malformed beat', _withGrid([{ n: 9, bpm: 127, t: 0 }, { n: 2, bpm: 127, t: 0.5 }])]
	]) {
		const deck = _deck({ anlz });
		assert.equal(grid.deckHasRealBeatGrid(deck), false, `${label} must read as no grid`);
		assert.equal(grid.effectiveQuantize(deck), false, `${label} must not quantize`);
		assert.equal(grid.effectiveBeatSync(deck), false, `${label} must not sync`);
	}
});

test('an empty deck is not blamed on the grid: nothing is loaded to analyse yet', () => {
	const empty = _deck({ stable_id: null });
	assert.equal(
		grid.gridFeaturesInert(empty),
		false,
		'if an empty deck reads as grid-inert then the tooltip tells a new user to ' +
			'analyse a track that was never loaded'
	);
});

//-----------------------------------------------------------------------------
// (b) transport source guards: no grid gate survives in the transport path
//-----------------------------------------------------------------------------

// e9493db18 (fix(rescue): schedule simultaneous play restore at shared context time, #2704)
// added startAtContextSec to play(), so every play() anchor below moved with the signature.
const TRANSPORT_ANCHORS = [
	'	async play(deck: DeckId, pressT0Ms?: number, startAtContextSec?: number): Promise<void> {',
	'	async pause(deck: DeckId, pressT0Ms?: number): Promise<void> {',
	'	async pressCue(deck: DeckId, pressT0Ms?: number): Promise<void> {',
	'	async quantizedSeek(deck: DeckId, ms: number, skipGridQuantize = false, pressT0Ms?: number): Promise<void> {',
	'	async setLoop(deck: DeckId, loop: { in_ms: number; out_ms: number } | null): Promise<void> {'
];

test('no transport method can refuse a deck for want of a beat grid', () => {
	for (const anchor of TRANSPORT_ANCHORS) {
		const body = engineBlockAfter(anchor);
		assert.ok(
			!body.includes('requireBeatGrid'),
			`if ${anchor.trim()} names requireBeatGrid then a gridless deck is refused there - ` +
				'the exact landmine identified in the lane A handover (4a.1)'
		);
	}
});

test('every transport quantize site reads the grid through the never-throwing helper', () => {
	for (const anchor of [
		'	async pause(deck: DeckId, pressT0Ms?: number): Promise<void> {',
		'	async pressCue(deck: DeckId, pressT0Ms?: number): Promise<void> {',
		'	async quantizedSeek(deck: DeckId, ms: number, skipGridQuantize = false, pressT0Ms?: number): Promise<void> {',
		'	private async _setLoop(\n\t\tdeck: DeckId, loop: { in_ms: number; out_ms: number } | null, beatLength: number | null\n\t): Promise<void> {'
	]) {
		const body = engineBlockAfter(anchor);
		assert.ok(
			body.includes('_quantizeGrid('),
			`if ${anchor.trim()} stops resolving its grid through _quantizeGrid then it is ` +
				'reading beats some other way and can throw on a gridless deck again'
		);
	}
});

test('_quantizeGrid returns null instead of throwing, which is what un-refuses pause', () => {
	const body = engineBlockAfter('function _quantizeGrid(st: DeckState): readonly AnlzBeat[] | null {');
	assert.ok(
		!body.includes('throw'),
		'if _quantizeGrid throws then pause is refusable again and a playing deck cannot be stopped'
	);
	assert.ok(
		body.includes('effectiveQuantize('),
		'if _quantizeGrid stops asking effectiveQuantize then its null case has drifted from ' +
			'what the UI calls inert'
	);
});

test('saving a hot cue asks for a USABLE grid, not merely a non-empty one', () => {
	const deckSrc = readFileSync(`${SRC}/lib/rb/deck-hot-cue-actions.ts`, 'utf8');
	const at = deckSrc.indexOf('async function saveHotCueAt(');
	assert.notEqual(at, -1, 'if saveHotCueAt moved then this guard is pointed at nothing');
	const body = deckSrc.slice(at, at + 900);
	assert.ok(
		body.includes('effectiveQuantize('),
		'if the hot-cue snap only checks beats.length then a one-beat or malformed grid ' +
			'clears the check and throws inside quantizeToNearestBeat, refusing the save'
	);
	assert.ok(
		!/beats\.length > 0/.test(body),
		'a length check is not a validity check: validateBeatGrid needs at least two ordered beats'
	);
});

test('play decides sync from the effective flag, not the raw one', () => {
	const body = engineBlockAfter('	async play(deck: DeckId, pressT0Ms?: number, startAtContextSec?: number): Promise<void> {');
	assert.ok(
		body.includes('effectiveBeatSync('),
		'if play reads st.beat_sync_enabled directly then a gridless deck with the default ' +
			'flag ON is routed into the sync join branch, which does require a grid'
	);
});

//-----------------------------------------------------------------------------
// (c) genuinely grid-dependent operations keep their requirement
//-----------------------------------------------------------------------------

test('beat loops and sync engagement still require a real grid', () => {
	for (const anchor of [
		'	async engageBeatLoop(deck: DeckId, beats: number, startMs?: number): Promise<void> {',
		'async function _synchronizeFollowers(\n' +
			'\tmaster: DeckId,\n' +
			'\tfollowers: readonly DeckId[],\n' +
			'\toptions: _SyncOptions = {}\n' +
			'): Promise<void> {'
	]) {
		const body = engineBlockAfter(anchor);
		assert.ok(
			body.includes('requireBeatGrid('),
			`if ${anchor.split('(')[0].trim()} drops requireBeatGrid then a grid-dependent ` +
				'operation runs against no grid and produces nonsense instead of an error'
		);
	}
});

test('a sync failure lands on the deck as sync_error, which the deck renders', () => {
	const body = engineBlockAfter(
		'async function _synchronizeFollowers(\n' +
			'\tmaster: DeckId,\n' +
			'\tfollowers: readonly DeckId[],\n' +
			'\toptions: _SyncOptions = {}\n' +
			'): Promise<void> {'
	);
	assert.ok(
		body.includes('sync_error = String(error)'),
		'if the catch stops stamping sync_error then a refused sync is visible only in ' +
			'command_error, which is exactly the burial this change removes'
	);
	const deckSrc = readFileSync(`${SRC}/lib/components/rb/Deck.svelte`, 'utf8');
	assert.ok(
		deckSrc.includes('deck.sync_error'),
		'if Deck.svelte stops reading sync_error then the engine has nowhere visible to report'
	);
});

//-----------------------------------------------------------------------------
// (d) engaging an inert toggle announces itself instead of erroring quietly
//-----------------------------------------------------------------------------

test('turning quantize or beat sync ON without a grid says so out loud', () => {
	for (const anchor of [
		'	setQuantize(deck: DeckId, enabled: boolean): void {',
		'	setBeatSync(deck: DeckId, enabled: boolean): Promise<void> {'
	]) {
		const body = engineBlockAfter(anchor);
		assert.ok(
			body.includes('gridFeaturesInert('),
			`if ${anchor.trim()} stops checking the grid then it either refuses or engages a no-op`
		);
		assert.ok(
			body.includes('pushToast('),
			`if ${anchor.trim()} stops announcing the inert state then the IPC and CLI paths ` +
				'flip a flag that does nothing, with no feedback at all'
		);
		assert.ok(
			body.includes('gridFeatureInertTip('),
			`if ${anchor.trim()} words the reason itself then the toast and the tooltip can drift`
		);
	}
});

test('setBeatSync still reverts the lit flag when a real phase lock is impossible', () => {
	// The pre-existing contract (863c0eb6) that this change must not weaken:
	// a GRIDDED deck that cannot lock still clears the flag and rethrows.
	const body = engineBlockAfter('	setBeatSync(deck: DeckId, enabled: boolean): Promise<void> {');
	const catchAt = body.indexOf('.catch(');
	assert.ok(catchAt > 0, 'if the catch is gone then BEAT SYNC can stay lit with no schedule');
	assert.ok(
		body.indexOf('st.beat_sync_enabled = false', catchAt) > catchAt,
		'if the revert is gone then the button stays lit after the failure propagates'
	);
});

//-----------------------------------------------------------------------------
// (e) the controls render inert with one shared, accurate tooltip
//-----------------------------------------------------------------------------

test('the gridless tip is one shared constant, and is NOT the PARITY-TODO wording', () => {
	assert.equal(typeof grid.GRID_FEATURE_TIP, 'string');
	assert.ok(grid.GRID_FEATURE_TIP.length > 20, 'the tip must actually explain itself');
	assert.notEqual(
		grid.GRID_FEATURE_TIP,
		PARITY_TODO_TITLE,
		'PARITY-TODO means "not built"; quantize and beat sync ARE built and simply have ' +
			'nothing to lock to on this track, which is a different fact'
	);
	assert.ok(
		!grid.GRID_FEATURE_TIP.includes('PARITY'),
		'if the tip borrows PARITY-TODO wording then inert-controls.test.mjs polices it as ' +
			'a drifted stub title'
	);
	assert.match(grid.GRID_FEATURE_TIP, /grid/i, 'the tip must name the missing thing');
});

test('the Q button goes inert with the shared tip when the loaded track has no grid', () => {
	assert.match(
		JOG_DIAL_SRC,
		/import \{[^}]*gridFeatureInertTip[^}]*\} from '\$lib\/player\/grid-features'/s,
		'if JogDial spells the tip itself then the wording can drift from DeckHeader'
	);
	assert.match(
		JOG_DIAL_SRC,
		/gridFeaturesInert\(deck\)/,
		'if the Q button stops asking gridFeaturesInert then it decides inertness its own way'
	);
	const qButton = _openTag(JOG_DIAL_SRC, 'data-performance-control="quantize"');
	assert.match(
		qButton,
		/disabled=\{[^}]*gridless[^}]*\}/,
		'if Q is not disabled on a gridless deck then a lit control does nothing when clicked'
	);
	assert.match(
		qButton,
		/title=\{qTitle\}/,
		'if Q stops taking its title from qTitle then the hover no longer explains the inert state'
	);
	assert.match(
		JOG_DIAL_SRC,
		/qTitle[\s\S]{0,200}gridInertTip/,
		'if qTitle stops resolving to the shared tip when gridless then the button is dead and silent'
	);
});

test('the BEAT SYNC button goes inert with the same shared tip', () => {
	assert.match(
		DECK_HEADER_SRC,
		/import \{[^}]*gridFeatureInertTip[^}]*\} from '\$lib\/player\/grid-features'/s,
		'if DeckHeader spells the tip itself then the wording can drift from JogDial'
	);
	assert.match(
		DECK_HEADER_SRC,
		/gridFeaturesInert\(deck\)/,
		'if BEAT SYNC stops asking gridFeaturesInert then it decides inertness its own way'
	);
	const syncButton = _openTag(DECK_HEADER_SRC, 'data-performance-control="beat-sync"');
	assert.match(
		syncButton,
		/disabled=\{[^}]*gridless[^}]*\}/,
		'if BEAT SYNC is not disabled on a gridless deck then it lights up and locks nothing'
	);
	assert.match(
		DECK_HEADER_SRC,
		/beatSyncTitle[\s\S]{0,300}gridInertTip/,
		'if beatSyncTitle stops resolving to the shared tip when gridless then the hover lies'
	);
});

test('the inert state clears the moment a gridded track lands on the deck', () => {
	const deck = _deck();
	assert.equal(grid.gridFeaturesInert(deck), true, 'gridless track: inert');
	deck.anlz = _withGrid();
	assert.equal(
		grid.gridFeaturesInert(deck),
		false,
		'if a real grid does not clear inertness then analysing a track leaves Q and BEAT SYNC dead'
	);
	assert.equal(grid.effectiveQuantize(deck), true);
	assert.equal(grid.effectiveBeatSync(deck), true);
});

//-----------------------------------------------------------------------------
// (f) a GRIDDED deck is completely unchanged
//-----------------------------------------------------------------------------

test('an off flag still wins on a gridded deck: the grid never turns a feature back on', () => {
	const deck = _deck({ anlz: _withGrid(), quantize_enabled: false, beat_sync_enabled: false });
	assert.equal(grid.deckHasRealBeatGrid(deck), true);
	assert.equal(grid.effectiveQuantize(deck), false);
	assert.equal(grid.effectiveBeatSync(deck), false);
});

test('a gridded deck still snaps to the exact PQTZ beat, ties going earlier', () => {
	// The unchanged parity behaviour, pinned live rather than by source text:
	// beats sit at 135ms / 608ms / 1080ms / 1553ms.
	assert.equal(engine.quantizedPositionMs(REAL_PQTZ_BEATS, 600, true), 608);
	assert.equal(engine.quantizedPositionMs(REAL_PQTZ_BEATS, 700, true), 608);
	assert.equal(engine.quantizedPositionMs(REAL_PQTZ_BEATS, 1000, true), 1080);
	assert.equal(
		engine.quantizedPositionMs(REAL_PQTZ_BEATS, 700, false),
		700,
		'quantize off must still pass the exact playhead through untouched'
	);
});

test('pause still stores a snapped cue when the deck does have a grid', () => {
	const body = engineBlockAfter('	async pause(deck: DeckId, pressT0Ms?: number): Promise<void> {');
	assert.ok(
		body.includes('quantizedPositionMs('),
		'if pause stops snapping then a gridded deck lost rekordbox parity on the memory cue'
	);
	assert.match(
		body,
		/pauseBeats !== null[\s\S]{0,160}quantizedPositionMs\(/,
		'if the snapped branch is no longer guarded by a resolved grid then pause is ' +
			'back to depending on one'
	);
});

test('LATENCY-02 QUANTIZED LAUNCH refuses on a gridless deck while plain play still runs', async () => {
	const gridless = _deck({ anlz: null });
	assert.equal(grid.effectiveBeatSync(gridless), false);
	const plan = await loadTypeScriptModule('src/lib/player/transport/quantized-launch.ts');
	const refused = plan.planQuantizedLaunch({
		nowContextTimeSec: 1,
		processorLeadSec: 0.05,
		masterPlaying: true,
		masterBeats: REAL_PQTZ_BEATS,
		masterPositionSec: 0.2,
		masterTempoRatio: 1,
		followerPlaying: false,
		followerBeats: [],
		followerPositionSec: 0
	});
	assert.equal(refused.kind, 'refuse');
	assert.match(refused.reason, /QUANTIZED LAUNCH/);
	const playBody = engineBlockAfter('async play(deck: DeckId, pressT0Ms?: number, startAtContextSec?: number): Promise<void> {');
	assert.doesNotMatch(playBody, /requireBeatGrid\(st, 'play'\)/);
});

//-----------------------------------------------------------------------------
// helpers
//-----------------------------------------------------------------------------

/** The full open tag of the element carrying `marker`, brace- and quote-aware
 * so an inline handler does not truncate it. */
function _openTag(source, marker) {
	const at = source.indexOf(marker);
	assert.notEqual(at, -1, `if ${marker} is gone then this guard is pointed at nothing`);
	const open = source.lastIndexOf('<', at);
	let depth = 0;
	let quote = null;
	for (let i = open; i < source.length; i += 1) {
		const ch = source[i];
		if (quote !== null) {
			if (ch === quote) quote = null;
			continue;
		}
		if (ch === '"' || ch === "'") quote = ch;
		else if (ch === '{') depth += 1;
		else if (ch === '}') depth -= 1;
		else if (ch === '>' && depth === 0) return source.slice(open, i + 1);
	}
	throw new Error(`unterminated open tag around ${marker}`);
}
