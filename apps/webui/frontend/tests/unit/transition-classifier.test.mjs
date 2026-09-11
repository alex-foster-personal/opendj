/**
 * TRANS-01: production classifyTransition on fixture mixer/transport timelines.
 *
 * [if] decks 1 and 2 are both active and the default mixer is at xfader 0.5
 *   with faders at 1 [then] state is transitioning
 * [if] the xfader then parks at 0 and only bus A remains heard [then] state
 *   is not transitioning on that snapshot
 * Detection is the production function. No stubbed transitioning.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { engineBlockAfter } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/transition-classifier.ts');
});

function deck(partial = {}) {
	const id = partial.id ?? 1;
	return {
		loaded: false,
		playing: false,
		audible: false,
		fader: 1,
		trim: 0.5,
		beat_sync_enabled: false,
		is_master: false,
		position_ms: 0,
		cue_ms: null,
		bpm: null,
		phrases: [],
		beatgrid: [],
		assign: id % 2 === 1 ? 'A' : 'B',
		...partial,
		id
	};
}

function input(partial = {}) {
	return {
		now_ms: 0,
		crossfader: 0.5,
		master: 1,
		master_muted: false,
		decks: [],
		...partial
	};
}

function livePair(partial = {}) {
	return input({
		decks: [
			deck({ id: 1, loaded: true, playing: true, audible: true }),
			deck({ id: 2, loaded: true, playing: true, audible: true })
		],
		...partial
	});
}

test('named constants lock the detection thresholds', () => {
	assert.equal(mod.HEARD_GAIN, 0.05);
	assert.equal(mod.APPROACHING_GAIN, 1e-4);
	assert.equal(mod.PHRASE_WINDOW_MS, 8000);
	assert.equal(mod.CUE_PLAY_WINDOW_MS, 2000);
	assert.equal(mod.XFADER_OFF_CENTER, 0.02);
});

test('dual-deck blend at the default mixer is transitioning', () => {
	const status = mod.classifyTransition(livePair());
	assert.equal(status.state, 'transitioning');
	assert.ok(status.outgoing_deck === 1 || status.outgoing_deck === 2);
	assert.ok(status.incoming_deck === 1 || status.incoming_deck === 2);
	assert.notEqual(status.incoming_deck, status.outgoing_deck);
});

test('parking the xfader at 0 after a blend is not transitioning', () => {
	assert.equal(mod.classifyTransition(livePair()).state, 'transitioning');
	const parked = mod.classifyTransition(livePair({ crossfader: 0 }));
	assert.notEqual(parked.state, 'transitioning');
	assert.equal(parked.outgoing_deck, 1);
});

test('parking the xfader at 1 after a blend is not transitioning', () => {
	const parked = mod.classifyTransition(livePair({ crossfader: 1 }));
	assert.notEqual(parked.state, 'transitioning');
	assert.equal(parked.outgoing_deck, 2);
});

test('same-bus channel-fader blend is transitioning', () => {
	const status = mod.classifyTransition(
		input({
			crossfader: 0,
			decks: [
				deck({ id: 1, loaded: true, playing: true, audible: true, assign: 'A' }),
				deck({ id: 2, loaded: true, playing: true, audible: true, assign: 'A' })
			]
		})
	);
	assert.equal(status.state, 'transitioning');
});

test('headphones cue with the incoming fader down is not transitioning', () => {
	const status = mod.classifyTransition(
		input({
			decks: [
				deck({ id: 1, loaded: true, playing: true, audible: true }),
				deck({ id: 2, loaded: true, playing: true, audible: true, fader: 0 })
			]
		})
	);
	assert.notEqual(status.state, 'transitioning');
});

test('two paused decks with faders up at xfader center are idle', () => {
	const status = mod.classifyTransition(
		input({
			decks: [
				deck({ id: 1, loaded: true, playing: false, audible: false }),
				deck({ id: 2, loaded: true, playing: false, audible: false })
			]
		})
	);
	assert.equal(status.state, 'idle');
	assert.equal(status.incoming_deck, null);
	assert.equal(status.outgoing_deck, null);
});

test('master muted during an otherwise live blend is idle', () => {
	assert.equal(mod.classifyTransition(livePair({ master_muted: true })).state, 'idle');
});

test('approaching via phrase window with a loaded incoming', () => {
	const status = mod.classifyTransition(
		input({
			crossfader: 0,
			decks: [
				deck({
					id: 1,
					loaded: true,
					playing: true,
					audible: true,
					position_ms: 92000,
					phrases: [{ start_ms: 0, end_ms: 100000 }]
				}),
				deck({ id: 2, loaded: true, playing: true, fader: 0 })
			]
		})
	);
	assert.equal(status.state, 'approaching');
	assert.equal(status.outgoing_deck, 1);
	assert.equal(status.incoming_deck, 2);
});

test('approaching via recent cue-to-play when incoming is not heard', () => {
	const status = mod.classifyTransition(
		input({
			crossfader: 0,
			decks: [
				deck({ id: 1, loaded: true, playing: true, audible: true }),
				deck({
					id: 2,
					loaded: true,
					playing: true,
					fader: 0,
					cue_ms: 1000,
					position_ms: 1500
				})
			]
		})
	);
	assert.equal(status.state, 'approaching');
	assert.equal(status.incoming_deck, 2);
});

test('xfader 0.04 opening bus B is approaching when incoming is not heard', () => {
	// 0.04 is off-center (> 0.02). Equal-power gain on B at 0.04 is ~0.0628,
	// which would already be heard if the incoming deck were playing with
	// fader 1, so this fixture keeps incoming unloaded-from-the-room (not
	// playing) and asserts approaching via the opening-bus rule.
	const status = mod.classifyTransition(
		input({
			crossfader: 0.04,
			decks: [
				deck({ id: 1, loaded: true, playing: true, audible: true }),
				deck({ id: 2, loaded: true, playing: false, audible: false })
			]
		})
	);
	assert.equal(status.state, 'approaching');
	assert.equal(status.incoming_deck, 2);
});

test('empty phrases and no other incoming signal stay idle', () => {
	const status = mod.classifyTransition(
		input({
			crossfader: 0,
			decks: [
				deck({ id: 1, loaded: true, playing: true, audible: true, phrases: [] }),
				deck({
					id: 2,
					loaded: true,
					playing: false,
					audible: false,
					beat_sync_enabled: false,
					phrases: [],
					beatgrid: []
				})
			]
		})
	);
	assert.equal(status.state, 'idle');
});

test('grid-only PQTZ fallback approaches when the next downbeat is in the window', () => {
	const beats = [];
	for (let i = 0; i < 16; i += 1) {
		beats.push({ n: (i % 4) + 1, time_ms: i * 500 });
	}
	const status = mod.classifyTransition(
		input({
			crossfader: 0,
			decks: [
				deck({
					id: 1,
					loaded: true,
					playing: true,
					audible: true,
					position_ms: 0,
					phrases: [],
					beatgrid: beats
				}),
				deck({
					id: 2,
					loaded: true,
					playing: false,
					beat_sync_enabled: false,
					phrases: [],
					beatgrid: []
				})
			]
		})
	);
	assert.equal(status.state, 'approaching');
	assert.equal(status.incoming_deck, 2);
});

test('dropping the incoming fader to 0 clears transitioning on the same snapshot', () => {
	assert.equal(mod.classifyTransition(livePair()).state, 'transitioning');
	const dropped = mod.classifyTransition(
		input({
			decks: [
				deck({ id: 1, loaded: true, playing: true, audible: true }),
				deck({ id: 2, loaded: true, playing: true, audible: true, fader: 0 })
			]
		})
	);
	assert.notEqual(dropped.state, 'transitioning');
});

test('the crossfade law here still matches the one the engine applies', () => {
	const engineLaw = engineBlockAfter(
		'function _xfGainFor(assign: CrossfaderAssign, x: number): number {'
	);
	for (const expression of [
		"if (assign === 'THRU') return 1;",
		'Math.cos((x * Math.PI) / 2)',
		'Math.cos(((1 - x) * Math.PI) / 2)'
	]) {
		assert.ok(
			engineLaw.includes(expression),
			`the engine no longer computes "${expression}"; re-derive the classifier`
		);
	}
});
