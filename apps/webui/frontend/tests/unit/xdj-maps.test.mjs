// requirement: CTRL-01 CTRL-11720
//   if AZ BEAT JUMP notes do not emit deck_beat_jump with the fixed beat sizes then broken
//   if RX3 gains deck_beat_jump bindings then broken
// Wire numbers are read off the official XDJ-RX3 and XDJ-AZ MIDI message lists.
//   if either map fails the core validator then broken
//   if play/cue/tempo/fader/PFL wires drift from the lists then broken
//   if AZ pads 5-8 leave channels 10-13 then broken
//   if a CC button stops decoding as press/release then broken
//   if RX3 binds a SHIFT wire the list does not give it then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
let vite;
let webmidi;
let maps;

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	maps = await vite.ssrLoadModule('/src/lib/rb/midi/maps/pioneer-xdj.ts');
});

after(async () => {
	await vite.close();
});

const one = (map, pred) => {
	const hits = map.bindings.filter(pred);
	assert.equal(hits.length, 1, `expected 1 binding, got ${hits.length}`);
	return hits[0];
};

test('both maps register and match only their own port', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(maps.XDJ_RX3_MAP);
	webmidi.registerDeviceMap(maps.XDJ_AZ_MAP);
	webmidi._resetMidiForTests();
	assert.ok(new RegExp(maps.XDJ_RX3_MAP.nameMatch, 'i').test('XDJ-RX3'));
	assert.ok(!new RegExp(maps.XDJ_RX3_MAP.nameMatch, 'i').test('XDJ-AZ'));
	assert.ok(new RegExp(maps.XDJ_AZ_MAP.nameMatch, 'i').test('XDJ-AZ'));
});

test('shared deck and mixer wires match both lists', () => {
	for (const map of [maps.XDJ_RX3_MAP, maps.XDJ_AZ_MAP]) {
		for (const deck of [1, 2, 3, 4]) {
			const play = one(map, (b) => b.action.type === 'deck_play_toggle' && b.action.deck === deck);
			assert.deepEqual(play.source, { ch: deck, kind: 'note', id: 0 });
			const tempo = one(map, (b) => b.action.type === 'deck_pitch' && b.action.deck === deck);
			assert.deepEqual(tempo.source, { ch: deck, kind: 'cc', id: 0 });
			assert.equal(tempo.action.lsbOffset, 32);
		}
		const fader4 = one(map, (b) => b.action.target === 'fader' && b.action.deck === 4);
		assert.deepEqual(fader4.source, { ch: 5, kind: 'cc', id: 20 });
		const low3 = one(map, (b) => b.action.band === 'low' && b.action.deck === 3);
		assert.deepEqual(low3.source, { ch: 5, kind: 'cc', id: 21 });
		const pfl = one(map, (b) => b.action.type === 'channel_cue' && b.action.deck === 2);
		assert.deepEqual(pfl.source, { ch: 5, kind: 'cc', id: 71 });
		assert.equal(pfl.ccButton, true);
	}
});

test('RX3 pads sit on channels 6-9 with notes 0-7 and 16-23', () => {
	const cue = one(maps.XDJ_RX3_MAP, (b) => b.action.slot === 'H' && b.action.deck === 2);
	assert.deepEqual(cue.source, { ch: 7, kind: 'note', id: 7 });
	const loop = one(maps.XDJ_RX3_MAP, (b) => b.action.beats === 32 && b.action.deck === 2);
	assert.deepEqual(loop.source, { ch: 7, kind: 'note', id: 23 });
});

test('AZ pads split across two channels with a 19-note stride', () => {
	const pad2 = one(maps.XDJ_AZ_MAP, (b) => b.action.slot === 'B' && b.action.deck === 1);
	assert.deepEqual(pad2.source, { ch: 6, kind: 'note', id: 19 });
	const pad8 = one(maps.XDJ_AZ_MAP, (b) => b.action.slot === 'H' && b.action.deck === 4);
	assert.deepEqual(pad8.source, { ch: 13, kind: 'note', id: 57 });
	const loop5 = one(
		maps.XDJ_AZ_MAP,
		(b) => b.action.type === 'deck_beat_loop' && b.source.ch === 10 && b.source.id === 2
	);
	assert.equal(loop5.action.beats, 4);
});

test('jog platter and side both bind centre-64 relative wires', () => {
	const rx3 = maps.XDJ_RX3_MAP.bindings.filter(
		(b) => b.action.type === 'deck_jog' && b.action.deck === 3
	);
	assert.deepEqual(rx3.map((b) => b.source.id).sort(), [33, 34]);
	for (const jog of rx3) {
		assert.equal(jog.source.ch, 3);
		assert.equal(jog.relative, true);
		assert.equal(jog.relativeCentered, true);
		assert.equal(jog.action.scale, undefined);
	}
	const az = maps.XDJ_AZ_MAP.bindings.filter(
		(b) => b.action.type === 'deck_jog' && b.action.deck === 3
	);
	assert.deepEqual(az.map((b) => b.source.id).sort(), [33, 34, 38, 41]);
	for (const jog of az) {
		assert.equal(jog.source.ch, 3);
		assert.equal(jog.relative, true);
		assert.equal(jog.relativeCentered, true);
	}
	const shifted = az.filter((b) => b.source.id === 38 || b.source.id === 41);
	assert.equal(shifted.length, 2);
	for (const jog of shifted) {
		assert.equal(jog.action.scale, 4);
	}
	const base = az.filter((b) => b.source.id === 33 || b.source.id === 34);
	for (const jog of base) {
		assert.equal(jog.action.scale, undefined);
	}
	assert.equal(webmidi.decodeRelativeCentered(0x41), 1);
	assert.equal(webmidi.decodeRelativeCentered(0x3f), -1);
});

test('jog scrub stops at the track ends and a bend stays in range', async () => {
	const jog = await vite.ssrLoadModule('/src/lib/rb/midi/jog.ts');
	// Base: 4 ticks * 2.5 ms = +10 ms (fixture pinned for CTRL-01 / #10284).
	assert.equal(jog.jogScrubTargetMs(1000, 5000, 4), 1010);
	assert.equal(jog.jogScrubTargetMs(1, 5000, -30), 0);
	assert.equal(jog.jogScrubTargetMs(4999, 5000, 30), 5000);
	assert.ok(Math.abs(jog.jogBendRatio(1, 5, 8) - 1.01) < 1e-9);
	assert.ok(Math.abs(jog.jogBendRatio(1, -60, 8) - 0.96) < 1e-9);
	assert.ok(Math.abs(jog.jogBendRatio(1.07, 60, 8) - 1.08) < 1e-9);
});

test('SHIFT exists only where the list gives it a wire', () => {
	const shifts = (map) => map.bindings.filter((b) => b.action.type === 'shift_modifier');
	assert.equal(shifts(maps.XDJ_RX3_MAP).length, 0);
	assert.equal(shifts(maps.XDJ_AZ_MAP).length, 4);
	assert.deepEqual(shifts(maps.XDJ_AZ_MAP)[0].source, { ch: 1, kind: 'note', id: 63 });
});

test('AZ BEAT JUMP wires bind deck_beat_jump on every deck; RX3 unchanged', () => {
	const beatJumpNotes = [
		[22, 1],
		[23, 4],
		[24, -1],
		[25, -4]
	];
	const hintSources = new Set(
		maps.XDJ_AZ_MAP.hints.map((h) => `${h.source.ch}:${h.source.kind}:${h.source.id}`)
	);
	for (const deck of [1, 2, 3, 4]) {
		for (const [note, beats] of beatJumpNotes) {
			const binding = one(
				maps.XDJ_AZ_MAP,
				(b) =>
					b.action.type === 'deck_beat_jump' &&
					b.action.deck === deck &&
					b.source.ch === deck &&
					b.source.id === note
			);
			assert.equal(binding.shift, undefined);
			assert.equal(binding.action.beats, beats);
			assert.equal(hintSources.has(`${deck}:note:${note}`), false);
		}
	}
	assert.equal(
		maps.XDJ_RX3_MAP.bindings.filter((b) => b.action.type === 'deck_beat_jump').length,
		0
	);
});

function wire(input, status, d1, d2) {
	assert.notEqual(input.onmidimessage, null);
	input.onmidimessage({ data: Uint8Array.of(status, d1, d2) });
}

test('AZ BEAT JUMP note bytes decode to deck_beat_jump actions', async () => {
	const actions = [];
	const azIn = {
		id: 'az-in',
		name: 'XDJ-AZ',
		manufacturer: 'AlphaTheta',
		state: 'connected',
		onmidimessage: null
	};
	const fakeAccess = {
		inputs: new Map([['az-in', azIn]]),
		outputs: new Map(),
		onstatechange: null
	};
	const request = async () => fakeAccess;
	const priorNav = globalThis.navigator;
	try {
		try {
			globalThis.navigator.requestMIDIAccess = request;
		} catch {
			Object.defineProperty(globalThis, 'navigator', {
				value: { requestMIDIAccess: request },
				configurable: true
			});
		}
		webmidi._resetMidiForTests();
		webmidi.registerDeviceMap(maps.XDJ_AZ_MAP);
		webmidi.registerActionHandler((action, value) => {
			actions.push({ action, value });
		});
		await webmidi.initMidi();
		wire(azIn, 0x90, 22, 0x7f);
		wire(azIn, 0x91, 24, 0x7f);
		wire(azIn, 0x90, 63, 0x7f);
		wire(azIn, 0x90, 23, 0x7f);
		wire(azIn, 0x93, 25, 0x7f);
		wire(azIn, 0x80, 22, 0x00);
		assert.deepEqual(actions[0].action, { type: 'deck_beat_jump', deck: 1, beats: 1 });
		assert.equal(actions[0].value.pressed, true);
		assert.deepEqual(actions[1].action, { type: 'deck_beat_jump', deck: 2, beats: -1 });
		assert.deepEqual(actions[2].action, { type: 'deck_beat_jump', deck: 1, beats: 4 });
		assert.deepEqual(actions[3].action, { type: 'deck_beat_jump', deck: 4, beats: -4 });
		assert.equal(actions[actions.length - 1].value.pressed, false);
	} finally {
		webmidi.unregisterActionHandler();
		webmidi._resetMidiForTests();
		if (priorNav) {
			try {
				globalThis.navigator.requestMIDIAccess = priorNav.requestMIDIAccess;
			} catch {
				Object.defineProperty(globalThis, 'navigator', { value: priorNav, configurable: true });
			}
		}
	}
});

// ---------------------------------------------------- jog wheel behaviour
//   if a wheel-speed tick burst sends one command per tick then broken
//   if summed ticks lose movement then broken
//   if a bend does not return to the base tempo then broken
//   if a sync follower is bent, or toasts per tick, then broken

const wait = (ms) => new Promise((done) => setTimeout(done, ms));

async function wheelRig(view) {
	const jog = await vite.ssrLoadModule('/src/lib/rb/midi/jog.ts');
	const seen = { seeks: [], tempos: [], refused: 0 };
	const deck = {
		playing: false,
		followsSyncMaster: false,
		pitch: 1,
		rangePct: 8,
		positionMs: 1000,
		durationMs: 60000,
		...view
	};
	const wheel = new jog.JogWheel(() => deck, {
		seek: (ms) => seen.seeks.push(ms),
		tempo: (ratio) => seen.tempos.push(ratio),
		bendRefused: () => (seen.refused += 1)
	});
	return { wheel, seen, deck };
}

test('a paused tick burst is batched and keeps all its movement', async () => {
	const { wheel, seen } = await wheelRig({});
	for (let tick = 0; tick < 200; tick++) wheel.turn(1);
	await wait(60);
	assert.equal(seen.seeks.length, 1);
	assert.equal(seen.seeks[0], 1500);
	wheel.turn(-4);
	await wait(60);
	assert.equal(seen.seeks[1], 1490);
});

test('a scrub follows the deck after an outside seek', async () => {
	const { wheel, seen, deck } = await wheelRig({});
	wheel.turn(4);
	await wait(40);
	deck.positionMs = 30000;
	wheel.turn(4);
	await wait(40);
	assert.deepEqual(seen.seeks, [1010, 30010]);
});

test('a bend lands, then returns to the tempo set during it', async () => {
	const { wheel, seen } = await wheelRig({ playing: true });
	wheel.turn(5);
	await wait(40);
	assert.ok(Math.abs(seen.tempos[0] - 1.01) < 1e-9);
	wheel.tempoMoved(1.02);
	await wait(120);
	assert.equal(seen.tempos.at(-1), 1.02);
	assert.equal(seen.seeks.length, 0);
});

test('a sync follower refuses the bend once per gesture', async () => {
	const { wheel, seen } = await wheelRig({ playing: true, followsSyncMaster: true });
	for (let round = 0; round < 3; round++) {
		wheel.turn(3);
		await wait(35);
	}
	await wait(120);
	assert.equal(seen.refused, 1);
	assert.equal(seen.tempos.length, 0);
});

test('a paused shifted tick scrubs 4x the base tick', async () => {
	const { wheel, seen } = await wheelRig({});
	wheel.turn(1, 4);
	await wait(60);
	assert.deepEqual(seen.seeks, [1010]);
	assert.equal(seen.tempos.length, 0);
});

test('a playing shifted tick bends exactly like the base tick', async () => {
	const { wheel, seen } = await wheelRig({ playing: true });
	wheel.turn(5, 4);
	await wait(40);
	// 5 ticks * 0.002 bend = +0.01, scale ignored while playing.
	assert.ok(Math.abs(seen.tempos[0] - 1.01) < 1e-9);
	assert.equal(seen.seeks.length, 0);
});

test('AZ hints cover every unbound documented pad row on all four decks', () => {
	const names = [
		'HOT CUE',
		'HOT CUE +SHIFT',
		'BEAT LOOP',
		'BEAT LOOP +SHIFT',
		'PAD FX1 PAGE1',
		'PAD FX1 PAGE2',
		'PAD FX2 PAGE1',
		'PAD FX2 PAGE2',
		'SLIP LOOP +SHIFT',
		'BEAT JUMP',
		'BEAT JUMP +SHIFT',
		'GATE CUE',
		'GATE CUE +SHIFT',
		'BEAT LOOP2',
		'BEAT LOOP2 +SHIFT',
		'KEY SHIFT',
		'KEY SHIFT +SHIFT',
		'BEAT JUMP2',
		'BEAT JUMP2 +SHIFT'
	];
	for (const deck of [1, 2, 3, 4])
		for (let pad = 0; pad < 8; pad++) {
			for (let offset = 0; offset < 19; offset++) {
				const ch = (pad < 4 ? 5 : 9) + deck;
				const id = 19 * (pad % 4) + offset;
				const matches = (r) => r.source.ch === ch && r.source.kind === 'note' && r.source.id === id;
				if (offset === 0 || offset === 2) {
					assert.equal(maps.XDJ_AZ_MAP.bindings.filter(matches).length, 1);
				} else {
					const hint = maps.XDJ_AZ_MAP.hints.filter(matches);
					assert.equal(hint.length, 1, `deck ${deck} pad ${pad + 1} offset ${offset}`);
					assert.equal(hint[0].label, `${names[offset]} PAD ${pad + 1} (deck ${deck})`);
					assert.equal(maps.XDJ_AZ_MAP.bindings.filter(matches).length, 0);
				}
			}
		}
});
