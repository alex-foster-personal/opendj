/**
 * DECKMOVE-01 deck move (the maintainer, Wed 4 Nov 2026): right-click a deck's artwork,
 * "Move track to deck N", and the target takes over the same audio, including
 * the live loop, so a repeating bassline on deck 1 carries on from deck 4.
 *
 * Industry reference: rekordbox / Serato "instant doubles" (clone track and
 * playhead onto another deck). This extends it with the live loop, the deck
 * transport and key settings, the channel strip, the stems and an audio-clock
 * handover crossfade, then stops the source.
 *
 * Regression lines:
 * - if a move leaves any listed state field (tempo, pitch range, key lock, key shift, KEY SYNC arm/mode/target, quantize, grid, sync mode, beat sync, slip, trim, eq, filter, color fx, stem eq mode, stem mutes/solos/gains, assign) different on the target then broken
 * - if the loop on the target differs in in_ms, out_ms or engaged state then broken
 * - if the target's playhead drifts more than 1 ms from the source's after the aligned start (loop wraps included) then broken
 * - if the handover curve does not run source level -> 0 and target 0 -> level, or its amplitudes do not sum to the level (a bump on identical audio) then broken
 * - if the default crossfade is not 8 beats at the playing tempo then broken
 * - if a move ends with the source still loaded or playing then broken
 * - if a move onto a playing deck runs without force, or a move from an empty deck or onto the same deck runs at all then broken
 * - if the opendj verb, the bus validator or the MCP tool lose deck_move, or the artwork menu stops listing every other deck, then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
	DECK_MOVE_DEFAULT_CROSSFADE_BEATS,
	DeckMoveRefused,
	captureDeckClone,
	deckMoveMenuEntries,
	handoverCurves,
	handoverDurationSec,
	handoverGains,
	moveDeck,
	refuseDeckMove
} from '../../src/lib/rb/deck-move.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';

const ROOT = fileURLToPath(new URL('../../', import.meta.url));
const REPO = fileURLToPath(new URL('../../../../../', import.meta.url));

let scheduleMath;
before(async () => {
	scheduleMath = await loadTypeScriptModule('src/lib/player/transport/schedule-math.ts');
});

const neutralStems = () => ({
	vocal: { muted: false, solo: false, gain: 0.5 },
	instrumental: { muted: false, solo: false, gain: 0.5 },
	drums: { muted: false, solo: false, gain: 0.5 },
	bass: { muted: false, solo: false, gain: 0.5 },
	other: { muted: false, solo: false, gain: 0.5 }
});
const stems = () => ({
	status: 'ready',
	controls: {
		...neutralStems(),
		vocal: { muted: true, solo: false, gain: 0.5 },
		instrumental: { muted: false, solo: false, gain: 0.7 },
		drums: { muted: false, solo: true, gain: 0.4 }
	},
	available_controls: ['vocal', 'instrumental', 'drums']
});

function sourceDeck(overrides = {}) {
	return {
		deck_id: 1, stable_id: 'trk-bass', duration_ms: 240000, position_ms: 61234.5, playing: true, bpm: 124,
		pitch: 1.0325, key_shift_semitones: -2, master_tempo_enabled: true, key_sync_enabled: false,
		key_sync_mode: 'relative', key_sync_target: 3,
		quantize_enabled: false, quantize_grid_beats: 8, beat_sync_enabled: true, sync_mode: 'bar', slip_enabled: true,
		loop: { in_ms: 60000.25, out_ms: 61935.733, engaged: true, beat_length: 4 }, is_master: true,
		stems: stems(), ...overrides
	};
}
const emptyDeck = (id, overrides = {}) => ({
	deck_id: id, stable_id: null, duration_ms: null, position_ms: 0, playing: false, bpm: null, pitch: 1,
	key_shift_semitones: 0, master_tempo_enabled: false, key_sync_enabled: false, key_sync_mode: 'exact',
	key_sync_target: null, quantize_enabled: true, quantize_grid_beats: 1, beat_sync_enabled: true, sync_mode: 'beat',
	slip_enabled: false, loop: null, is_master: false,
	stems: { status: 'unavailable', controls: neutralStems(), available_controls: [] }, ...overrides
});
const channel = (id, overrides = {}) => ({
	deck_id: id, trim: 0.62, eq_high: 0.31, eq_mid: 0.55, eq_low: 0.12, filter: 0.7, fader: 0.85, assign: 'A',
	cue_enabled: true, stem_eq_mode: true, ...overrides
});
const flatChannel = (id) => ({
	deck_id: id, trim: 0.5, eq_high: 0.5, eq_mid: 0.5, eq_low: 0.5, filter: 0.5, fader: 1, assign: 'B',
	cue_enabled: false, stem_eq_mode: false
});
const playingTarget = () => ({ ...emptyDeck(4), stable_id: 'other', duration_ms: 1000, bpm: 100, playing: true });

/** A test double of the engine port: the real engine's state shapes, recording every call. */
function fakePort({ from = 1, to = 4, source = sourceDeck(), target, srcChannel, dstChannel, lateBySec = 0 } = {}) {
	const decks = { 1: emptyDeck(1), 2: emptyDeck(2), 3: emptyDeck(3), 4: emptyDeck(4) };
	decks[from] = { ...source, deck_id: from };
	if (target !== undefined) decks[to] = target;
	const channels = { 1: flatChannel(1), 2: flatChannel(2), 3: flatChannel(3), 4: flatChannel(4) };
	channels[from] = srcChannel ?? channel(from);
	if (dstChannel !== undefined) channels[to] = dstChannel;
	const fx = {};
	for (const d of [1, 2, 3, 4]) fx[d] = { effect: 'filter', echo_beats: 0.5 };
	fx[from] = { effect: 'echo', echo_beats: 0.75 };
	const ranges = { 1: 8, 2: 8, 3: 8, 4: 8 };
	ranges[from] = 16;
	const calls = [];
	const rec = (name, ...args) => calls.push([name, ...args]);
	return {
		calls, decks, channels, fx, ranges,
		deck: (d) => decks[d],
		channel: (d) => channels[d],
		colorFx: (d) => fx[d],
		pitchRange: (d) => ranges[d],
		masterLocked: () => false,
		async load(d, sid) { rec('load', d, sid); decks[d] = { ...emptyDeck(d), stable_id: sid, duration_ms: source.duration_ms, bpm: source.bpm }; },
		async unload(d) { rec('unload', d); decks[d] = emptyDeck(d); },
		async pause(d) { rec('pause', d); decks[d].playing = false; },
		setPitchRange(d, r) { rec('setPitchRange', d, r); ranges[d] = r; },
		async setTempoRatio(d, r) { rec('setTempoRatio', d, r); decks[d].pitch = r; },
		async setMasterTempo(d, v) { rec('setMasterTempo', d, v); decks[d].master_tempo_enabled = v; },
		async setKeyShift(d, s) { rec('setKeyShift', d, s); decks[d].key_shift_semitones = s; decks[d].key_sync_enabled = false; },
		async setKeySync(d, enabled, options) {
			rec('setKeySync', d, enabled, options);
			// The engine derives the shift from the leader; same track + same leader = the source's shift.
			decks[d].key_sync_enabled = enabled;
			decks[d].key_sync_mode = options.mode;
			decks[d].key_sync_target = options.target === 'master' ? null : options.target;
			if (enabled) decks[d].key_shift_semitones = decks[from].key_shift_semitones;
		},
		setQuantize(d, v) { rec('setQuantize', d, v); decks[d].quantize_enabled = v; },
		setQuantizeGrid(d, v) { rec('setQuantizeGrid', d, v); decks[d].quantize_grid_beats = v; },
		async setSyncMode(d, v) { rec('setSyncMode', d, v); decks[d].sync_mode = v; },
		async setBeatSync(d, v) { rec('setBeatSync', d, v); decks[d].beat_sync_enabled = v; },
		async setSlip(d, v) { rec('setSlip', d, v); decks[d].slip_enabled = v; },
		async setLoop(d, loop, quantize) { rec('setLoop', d, loop, quantize); decks[d].loop = { ...loop, engaged: true, beat_length: null }; },
		setTrim(d, v) { rec('setTrim', d, v); channels[d].trim = v; },
		setEq(d, band, v) { rec('setEq', d, band, v); channels[d][`eq_${band}`] = v; },
		setFilter(d, v) { rec('setFilter', d, v); channels[d].filter = v; },
		applyColorFx(d, s) { rec('applyColorFx', d, s); fx[d] = { ...s }; },
		setStemEqMode(d, v) { rec('setStemEqMode', d, v); channels[d].stem_eq_mode = v; },
		async waitStemsReady(d) { rec('waitStemsReady', d); decks[d].stems = { ...stems(), controls: neutralStems() }; },
		setStemMute(d, s, v) { rec('setStemMute', d, s, v); decks[d].stems.controls[s].muted = v; },
		setStemSolo(d, s, v) { rec('setStemSolo', d, s, v); decks[d].stems.controls[s].solo = v; },
		setStemGain(d, s, v) { rec('setStemGain', d, s, v); decks[d].stems.controls[s].gain = v; },
		setFader(d, v) { rec('setFader', d, v); channels[d].fader = v; },
		assignChannel(d, v) { rec('assignChannel', d, v); channels[d].assign = v; },
		async seekPaused(d, ms) { rec('seekPaused', d, ms); decks[d].position_ms = ms; },
		async startAlignedTo(d, f) { rec('startAlignedTo', d, f); decks[d].playing = true; return 12.5; },
		scheduleHandover(f, t, start, dur, level) { rec('scheduleHandover', f, t, start, dur, level); return start + lateBySec; },
		async waitUntilContextSec(sec) { rec('waitUntilContextSec', sec); },
		async setDeckMaster(d, o) { rec('setDeckMaster', d, o); }
	};
}

const MOVE = { from: 1, to: 4, crossfade_beats: 8, mode: 'move' };
const CLONED_DECK_FIELDS = ['stable_id', 'pitch', 'key_shift_semitones', 'master_tempo_enabled', 'key_sync_enabled',
	'key_sync_mode', 'key_sync_target', 'quantize_enabled', 'quantize_grid_beats', 'beat_sync_enabled', 'sync_mode',
	'slip_enabled'];
const CLONED_CHANNEL_FIELDS = ['trim', 'eq_high', 'eq_mid', 'eq_low', 'filter', 'stem_eq_mode', 'assign'];

function assertClonedEverything(port, before) {
	for (const key of CLONED_DECK_FIELDS) assert.deepEqual(port.decks[4][key], before.deck[key], `deck field ${key}`);
	for (const key of CLONED_CHANNEL_FIELDS) assert.deepEqual(port.channels[4][key], before.channel[key], `channel field ${key}`);
	assert.deepEqual(port.fx[4], before.fx, 'color fx effect and echo division');
	assert.equal(port.ranges[4], 16, 'pitch range');
	for (const stem of before.deck.stems.available_controls) {
		assert.deepEqual(port.decks[4].stems.controls[stem], before.deck.stems.controls[stem], `stem ${stem}`);
	}
	assert.equal(port.channels[4].fader, before.channel.fader, 'the target ends at the source fader level');
}

const snapshot = (port) => structuredClone({ deck: port.decks[1], channel: port.channels[1], fx: port.fx[1] });

test('a move clones every listed state field onto the target (manual key shift)', async () => {
	const port = fakePort();
	const before = snapshot(port);
	await moveDeck(port, MOVE);
	assertClonedEverything(port, before);
	assert.ok(port.calls.some((c) => c[0] === 'setKeyShift' && c[1] === 4 && c[2] === -2), 'KEYSYNC-01 absolute key shift');
});

test('a move clones an armed KEY SYNC (mode and target) and lets it derive the shift', async () => {
	const port = fakePort({ source: sourceDeck({ key_sync_enabled: true, key_sync_mode: 'compatible', key_sync_target: 2, key_shift_semitones: 3 }) });
	const before = snapshot(port);
	await moveDeck(port, MOVE);
	assertClonedEverything(port, before);
	assert.equal(port.calls.some((c) => c[0] === 'setKeyShift'), false, 'a manual shift would disarm the follow');
	assert.deepEqual(port.calls.find((c) => c[0] === 'setKeySync').slice(1), [4, true, { mode: 'compatible', target: 2 }]);
});

test('a source that KEY SYNCs to the target deck hands the target MASTER instead (a deck cannot follow itself)', async () => {
	const port = fakePort({ source: sourceDeck({ key_sync_target: 4 }) });
	await moveDeck(port, MOVE);
	assert.deepEqual(port.calls.find((c) => c[0] === 'setKeySync').slice(1), [4, false, { mode: 'relative', target: 'master' }]);
});

test('the live loop carries: identical in/out points, engaged, unquantized, before the target starts', async () => {
	const port = fakePort();
	const result = await moveDeck(port, MOVE);
	const setLoop = port.calls.find((c) => c[0] === 'setLoop');
	assert.deepEqual(setLoop, ['setLoop', 4, { in_ms: 60000.25, out_ms: 61935.733 }, false]);
	assert.ok(port.calls.indexOf(setLoop) < port.calls.findIndex((c) => c[0] === 'startAlignedTo'));
	assert.equal(port.decks[4].loop.in_ms, 60000.25);
	assert.equal(port.decks[4].loop.out_ms, 61935.733);
	assert.equal(port.decks[4].loop.engaged, true);
	assert.equal(result.loop, 'engaged');
});

test('a disengaged stored loop is not engaged on the target', async () => {
	const port = fakePort({ source: sourceDeck({ loop: { in_ms: 1, out_ms: 2, engaged: false, beat_length: null } }) });
	const result = await moveDeck(port, MOVE);
	assert.equal(port.calls.some((c) => c[0] === 'setLoop'), false);
	assert.equal(result.loop, 'stored_not_engaged');
});

test('a playing source starts the target on the source clock, not from a published position', async () => {
	const port = fakePort();
	await moveDeck(port, MOVE);
	assert.ok(port.calls.some((c) => c[0] === 'startAlignedTo' && c[1] === 4 && c[2] === 1));
	assert.equal(port.calls.some((c) => c[0] === 'seekPaused'), false);
});

test('the aligned target stays phase-locked to the source within 1 ms, across loop wraps', () => {
	// What startDeckAlignedTo schedules: the target segment opens at the start
	// instant `when` on the source's own projected position, at the source's tempo,
	// with the cloned loop. Both are then read through the real projection.
	const { _positionForSegment } = scheduleMath;
	const durationSec = 240;
	const loop = { in_ms: 60000.25, out_ms: 61935.733, engaged: true, beat_length: 4 };
	const tempoRatio = 1.0325;
	const source = { active: true, loop, startContextTime: 3.217, startPositionSec: 58.4, tempoRatio };
	const when = 12.5 + 1 / 48000;
	const target = { active: true, loop, startContextTime: when, startPositionSec: _positionForSegment(source, when, durationSec), tempoRatio };
	let wraps = 0, last = target.startPositionSec;
	for (let t = when; t < when + 60; t += 0.0103) {
		const src = _positionForSegment(source, t, durationSec), dst = _positionForSegment(target, t, durationSec);
		if (dst < last) wraps += 1;
		last = dst;
		// The loop seam is one point mod (out - in); measure the error on that circle.
		const span = (loop.out_ms - loop.in_ms) / 1000;
		const raw = Math.abs(src - dst), errSec = Math.min(raw, Math.abs(raw - span));
		assert.ok((errSec / tempoRatio) * 1000 < 1, `phase error ${(errSec * 1000).toFixed(4)} ms at context ${t}`);
	}
	assert.ok(wraps >= 20, `the probe crossed the loop seam ${wraps} times`);
	const engine = readFileSync(`${ROOT}src/lib/rb/audio-engine.svelte.ts`, 'utf8');
	const body = engine.slice(engine.indexOf('export async function startDeckAlignedTo('), engine.indexOf('export function scheduleDeckHandover('));
	assert.match(body, /return _projectPositionAt\(source, effectiveWhen\)/, 'the engine starts the target at the source projection for the instant the schedule lands');
});

test('a paused source parks the target at the same position and swaps without a fade', async () => {
	const port = fakePort({ source: sourceDeck({ playing: false }) });
	await moveDeck(port, MOVE);
	assert.ok(port.calls.some((c) => c[0] === 'seekPaused' && c[1] === 4 && c[2] === 61234.5));
	assert.equal(port.calls.some((c) => c[0] === 'startAlignedTo'), false);
	assert.equal(port.decks[1].stable_id, null);
	assert.equal(port.channels[4].fader, 0.85);
});

test('the crossfade curve hits its endpoints, and its amplitudes sum to the level (no bump on identical audio)', () => {
	for (let i = 0; i <= 100; i += 1) {
		const { source, target } = handoverGains(i / 100);
		assert.ok(Math.abs(source + target - 1) < 1e-12, `sum at ${i}`);
	}
	assert.deepEqual(handoverGains(0), { source: 1, target: 0 });
	const end = handoverGains(1);
	assert.ok(end.source < 1e-15 && end.target === 1);
	// sqrt of each gain is the cos/sin equal-power pair.
	assert.ok(Math.abs(Math.sqrt(handoverGains(0.5).source) - Math.cos(Math.PI / 4)) < 1e-12);
	const curves = handoverCurves(0.8, 65);
	assert.equal(curves.source[0], Math.fround(0.8));
	assert.equal(curves.target[0], 0);
	assert.ok(Math.abs(curves.target[64] - 0.8) < 1e-6);
	assert.ok(curves.source[64] < 1e-6);
	for (let i = 0; i < 65; i += 1) assert.ok(Math.abs(curves.source[i] + curves.target[i] - 0.8) < 1e-6);
});

test('the crossfade is 8 beats by default at the playing tempo; 0 beats is a cut', () => {
	assert.equal(DECK_MOVE_DEFAULT_CROSSFADE_BEATS, 8);
	assert.ok(Math.abs(handoverDurationSec(8, 120, 1) - 4) < 1e-12);
	assert.ok(Math.abs(handoverDurationSec(8, 120, 1.25) - 3.2) < 1e-12);
	assert.equal(handoverDurationSec(0, null, 1), 0);
	assert.throws(() => handoverDurationSec(8, null, 1), DeckMoveRefused);
});

test('a move runs the handover on the audio clock and ends with the source stopped and unloaded', async () => {
	const port = fakePort();
	const result = await moveDeck(port, MOVE);
	const handover = port.calls.find((c) => c[0] === 'scheduleHandover');
	const duration = (8 * 60) / (124 * 1.0325);
	assert.deepEqual(handover.slice(0, 4), ['scheduleHandover', 1, 4, 12.5]);
	assert.ok(Math.abs(handover[4] - duration) < 1e-12);
	assert.equal(handover[5], 0.85, 'the target ends at the source fader level');
	const waitAt = port.calls.findIndex((c) => c[0] === 'waitUntilContextSec');
	assert.ok(port.calls[waitAt][1] >= 12.5 + duration);
	const pauseAt = port.calls.findIndex((c) => c[0] === 'pause' && c[1] === 1);
	const unloadAt = port.calls.findIndex((c) => c[0] === 'unload' && c[1] === 1);
	assert.ok(waitAt < pauseAt && pauseAt < unloadAt, 'stop only after the source gain reached 0');
	assert.equal(port.decks[1].stable_id, null);
	assert.equal(port.decks[1].playing, false);
	assert.equal(port.decks[4].playing, true, 'the target keeps playing');
	assert.equal(port.channels[1].fader, 0.85, 'the source strip is left where the hand left it');
	assert.ok(port.calls.some((c) => c[0] === 'setDeckMaster' && c[1] === 4), 'master follows the audio');
	assert.equal(result.start_context_sec, 12.5);
	assert.ok(Math.abs(result.crossfade_sec - duration) < 1e-12);
});

test('a handover the await chain started late still waits out the whole fade before stopping the source', async () => {
	const port = fakePort({ lateBySec: 0.3 });
	await moveDeck(port, MOVE);
	const duration = (8 * 60) / (124 * 1.0325);
	const wait = port.calls.find((c) => c[0] === 'waitUntilContextSec');
	assert.ok(wait[1] >= 12.5 + 0.3 + duration, `waited only to ${wait[1]}`);
});

test('the target fader is closed before the target starts, so there is no double-level bump', async () => {
	const port = fakePort();
	await moveDeck(port, MOVE);
	const closeAt = port.calls.findIndex((c) => c[0] === 'setFader' && c[1] === 4 && c[2] === 0);
	const startAt = port.calls.findIndex((c) => c[0] === 'startAlignedTo');
	assert.ok(closeAt >= 0 && closeAt < startAt);
});

test('copy leaves the source playing and the target fader alone (instant doubles)', async () => {
	const port = fakePort({ dstChannel: { ...flatChannel(4), fader: 0.3 } });
	await moveDeck(port, { ...MOVE, mode: 'copy' });
	assert.equal(port.decks[1].stable_id, 'trk-bass');
	assert.equal(port.decks[1].playing, true);
	assert.equal(port.calls.some((c) => c[0] === 'scheduleHandover'), false);
	assert.equal(port.channels[4].fader, 0.3);
});

test('refusals: same deck, empty source, a playing target without force, a bad crossfade', () => {
	const port = fakePort({ target: playingTarget() });
	const code = (fn) => {
		try { fn(); } catch (e) { assert.ok(e instanceof DeckMoveRefused, String(e)); return e.code; }
		return null;
	};
	assert.equal(code(() => refuseDeckMove(port, { ...MOVE, to: 1 })), 'same_deck');
	assert.equal(code(() => refuseDeckMove(port, { ...MOVE, from: 2, to: 3 })), 'source_not_loaded');
	assert.equal(code(() => refuseDeckMove(port, MOVE)), 'target_playing');
	assert.equal(code(() => refuseDeckMove(port, { ...MOVE, force: true })), null);
	assert.equal(code(() => refuseDeckMove(port, { ...MOVE, crossfade_beats: -1, force: true })), 'bad_crossfade');
	const noAudio = fakePort({ source: sourceDeck({ duration_ms: null }) });
	assert.equal(code(() => refuseDeckMove(noAudio, MOVE)), 'source_not_loaded');
});

test('a refused move touches nothing and names the force override', async () => {
	const port = fakePort({ target: playingTarget() });
	await assert.rejects(moveDeck(port, MOVE), (e) => e.code === 'target_playing' && /force/.test(e.message));
	assert.deepEqual(port.calls, []);
});

test('force ejects a playing target first; a paused loaded target is replaced without force', async () => {
	const forced = fakePort({ target: playingTarget() });
	const result = await moveDeck(forced, { ...MOVE, force: true });
	assert.deepEqual(forced.calls[0], ['unload', 4]);
	assert.equal(result.replaced_stable_id, 'other');
	const parked = fakePort({ target: { ...playingTarget(), playing: false } });
	await moveDeck(parked, MOVE);
	assert.deepEqual(parked.calls[0], ['unload', 4]);
	assert.equal(parked.decks[4].stable_id, 'trk-bass');
});

test('captureDeckClone snapshots values, not live references', () => {
	const port = fakePort();
	const plan = captureDeckClone(port, 1);
	port.decks[1].loop.in_ms = 0;
	port.channels[1].trim = 0;
	assert.equal(plan.loop.in_ms, 60000.25);
	assert.equal(plan.channel.trim, 0.62);
});

test('parity: bus validator, opendj verb, MCP tool and artwork menu all reach the one moveDeck', () => {
	const ipc = readFileSync(`${ROOT}src/lib/rb/performance-ipc.svelte.ts`, 'utf8');
	assert.match(ipc, /type: 'deck_move'; from: DeckId; to: DeckId/);
	assert.match(ipc, /type === 'deck_move'/);
	assert.match(ipc, /moveDeck\(webAudioDeckMovePort\(\)/);
	assert.match(readFileSync(`${REPO}apps/opendj_cli/catalog.py`, 'utf8'), /Verb\(\s*"deck_move",\s*"deck_move"/);
	assert.match(readFileSync(`${REPO}apps/opendj_cli/mcp_deck_tools.py`, 'utf8'), /def deck_move\(/);
});

test('the artwork menu offers "Move track to deck N" for every other deck and forces only a named playing target', () => {
	const view = { 1: { stable_id: 'trk-bass', title: 'Bassline', playing: true }, 2: { stable_id: null, title: null, playing: false },
		3: { stable_id: 'cued', title: 'Cued', playing: false }, 4: { stable_id: 'live', title: 'Live', playing: true } };
	assert.deepEqual(deckMoveMenuEntries(1, [1, 2, 3, 4], (d) => view[d]), [
		{ to: 2, label: 'Move track to deck 2', force: false },
		{ to: 3, label: 'Move track to deck 3 (replaces Cued)', force: false },
		{ to: 4, label: 'Move track to deck 4 (replaces Live, playing)', force: true }
	]);
});
