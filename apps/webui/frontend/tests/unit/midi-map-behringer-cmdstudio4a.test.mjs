// requirement: MIDI-11991
// Unit tests for the Behringer CMD Studio 4a device map
// (src/lib/rb/midi/maps/behringer-cmdstudio4a.ts).
//
// Regression lines:
//   if registerDeviceMap(BEHRINGER_CMD_STUDIO_4A_MAP) throws then broken
//   if nameMatch claims CMD Studio 2a, MM-1 or other CMD units then broken
//   if any bound or hinted (ch, kind, id) is absent from the [CHART] allow-list then broken
//   if play, cue, sync, faders, crossfader or jogs are unbound then broken
//   if FX knob 4 is not the colour FX filter on all four decks then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let vite;
let webmidi;
let mod;
let map;

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
	mod = await vite.ssrLoadModule('/src/lib/rb/midi/maps/behringer-cmdstudio4a.ts');
	map = mod.BEHRINGER_CMD_STUDIO_4A_MAP;
});

after(async () => {
	await vite.close();
});

// ------------------------------------------------- [CHART] allow-list

const key = (s) => `${s.ch}:${s.kind}:${s.id}`;

const _LEFT_NOTES = [
	43, 44, 45, 34, 35, 36, 37, 38, 39, 40, 41, 42, 23, 24, 25, 80, 82, 83, 16, 17, 18, 19, 22, 27,
	26, 32, 33, 96, 97, 98, 106
];
const _LEFT_CC = [16, 17, 18, 19, 26, 96, 97, 98, 112];

const _RIGHT_NOTES = [
	75, 76, 77, 66, 67, 68, 69, 70, 71, 72, 73, 74, 56, 55, 57, 81, 84, 85, 48, 49, 50, 51, 54, 59,
	58, 64, 65, 99, 100, 101, 107
];
const _RIGHT_CC = [48, 49, 50, 51, 58, 99, 100, 101, 113];

const _GLOBAL_NOTES = [1, 2, 3];
const _GLOBAL_CC = [1, 47, 114, 122, 123];

/** Every INPUT row of the Behringer CMD chart, as (ch, kind, ids). */
const CHART_INPUT = new Set(
	[
		...[1, 3].flatMap((ch) => [
			..._LEFT_NOTES.map((id) => key({ ch, kind: 'note', id })),
			..._LEFT_CC.map((id) => key({ ch, kind: 'cc', id })),
			key({ ch, kind: 'pitchbend', id: 0 })
		]),
		...[2, 4].flatMap((ch) => [
			..._RIGHT_NOTES.map((id) => key({ ch, kind: 'note', id })),
			..._RIGHT_CC.map((id) => key({ ch, kind: 'cc', id })),
			key({ ch, kind: 'pitchbend', id: 0 })
		]),
		..._GLOBAL_NOTES.map((id) => key({ ch: 1, kind: 'note', id })),
		..._GLOBAL_CC.map((id) => key({ ch: 1, kind: 'cc', id }))
	].flat()
);

const _LEFT_LED_NOTES = [44, 45, 106, 23, 24, 34, 35, 36, 37, 38, 39, 40, 41];
const _RIGHT_LED_NOTES = [76, 77, 107, 56, 55, 66, 67, 68, 69, 70, 71, 72, 73];

/** Every OUTPUT note row of the chart that an LED rule may drive. */
const CHART_OUTPUT = new Set(
	[
		...[1, 3].flatMap((ch) => _LEFT_LED_NOTES.map((id) => key({ ch, kind: 'note', id }))),
		...[2, 4].flatMap((ch) => _RIGHT_LED_NOTES.map((id) => key({ ch, kind: 'note', id })))
	].flat()
);

const find = (pred) => map.bindings.filter(pred);
const at = (ch, kind, id) => (b) =>
	b.source.ch === ch && b.source.kind === kind && b.source.id === id;

// --------------------------------------------------------------- tests

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(map);
	webmidi._resetMidiForTests();
});

test('nameMatch claims the CMD Studio 4a only', () => {
	const re = new RegExp(map.nameMatch, 'i');
	for (const name of [
		'CMD Studio 4a',
		'CMD STUDIO 4a',
		'CMDStudio4a',
		'CMD Studio 4a MIDI 1',
		'Behringer CMDStudio4a'
	])
		assert.ok(re.test(name), name);
	for (const name of [
		'CMD Studio 2a',
		'CMD Studio 4',
		'CMD Studio 4ab',
		'CMD MM-1',
		'CMD PL-1',
		'CMD DC-1',
		'CMD Micro'
	])
		assert.ok(!re.test(name), name);
});

test('every binding, hint and LED wire is in the chart', () => {
	for (const b of map.bindings)
		assert.ok(CHART_INPUT.has(key(b.source)), `binding ${key(b.source)}`);
	for (const h of map.hints) assert.ok(CHART_INPUT.has(key(h.source)), `hint ${key(h.source)}`);
	for (const l of map.leds)
		assert.ok(
			CHART_OUTPUT.has(key({ ch: l.out.ch, kind: 'note', id: l.out.note })),
			`led ch${l.out.ch} note ${l.out.note}`
		);
});

test('play, cue, sync, fader and jog are bound per deck; crossfader global', () => {
	for (const [ch, deck, play, cue, sync, faderCc, jogCc] of [
		[1, 1, 44, 43, 45, 112, 26],
		[2, 2, 76, 75, 77, 113, 58],
		[3, 3, 44, 43, 45, 112, 26],
		[4, 4, 76, 75, 77, 113, 58]
	]) {
		for (const [id, type] of [
			[play, 'deck_play_toggle'],
			[cue, 'deck_cue'],
			[sync, 'deck_sync_toggle']
		]) {
			const hits = find((b) => b.action.type === type && b.action.deck === deck);
			assert.equal(hits.length, 1, `${type} deck ${deck}`);
			assert.ok(at(ch, 'note', id)(hits[0]));
		}
		const fader = find(
			(b) =>
				b.action.type === 'mixer_channel' && b.action.deck === deck && b.action.target === 'fader'
		);
		assert.equal(fader.length, 1, `fader deck ${deck}`);
		assert.ok(at(ch, 'cc', faderCc)(fader[0]));
		const jogs = find((b) => b.action.type === 'deck_jog' && b.action.deck === deck);
		assert.equal(jogs.length, 1, `jog deck ${deck}`);
		assert.ok(
			jogs[0].relative === true &&
				jogs[0].relativeCentered === true &&
				jogs[0].source.ch === ch &&
				jogs[0].source.id === jogCc
		);
	}
	assert.equal(webmidi.decodeRelativeCentered(0x41), 1);
	assert.equal(webmidi.decodeRelativeCentered(0x3f), -1);
	const xf = find((b) => b.action.type === 'mixer_global' && b.action.target === 'crossfader');
	assert.equal(xf.length, 1);
	assert.ok(at(1, 'cc', 114)(xf[0]));
});

test('colour FX filter on FX knob 4; other FX controls hinted', () => {
	for (const [ch, filterCc, fxKnobs, fxBtns, fxAssign] of [
		[1, 19, [16, 17, 18], [16, 17, 18, 19], [82, 83]],
		[2, 51, [48, 49, 50], [48, 49, 50, 51], [84, 85]],
		[3, 19, [16, 17, 18], [16, 17, 18, 19], [82, 83]],
		[4, 51, [48, 49, 50], [48, 49, 50, 51], [84, 85]]
	]) {
		const f = find(at(ch, 'cc', filterCc));
		assert.equal(f.length, 1, `filter ch ${ch}`);
		assert.equal(f[0].action.target, 'filter');
		for (const id of fxKnobs) {
			assert.equal(find(at(ch, 'cc', id)).length, 0);
			assert.ok(map.hints.some((h) => key(h.source) === key({ ch, kind: 'cc', id })));
		}
		for (const id of [...fxBtns, ...fxAssign]) {
			assert.equal(find(at(ch, 'note', id)).length, 0);
			assert.ok(map.hints.some((h) => key(h.source) === key({ ch, kind: 'note', id })));
		}
	}
	assert.equal(map.hints.length, 79);
});

test('EQ bands, pitchbend and browse spot checks', () => {
	for (const [ch, high, mid, low] of [
		[1, 96, 97, 98],
		[2, 99, 100, 101],
		[3, 96, 97, 98],
		[4, 99, 100, 101]
	]) {
		assert.equal(find(at(ch, 'cc', high))[0].action.band, 'high');
		assert.equal(find(at(ch, 'cc', mid))[0].action.band, 'mid');
		assert.equal(find(at(ch, 'cc', low))[0].action.band, 'low');
		const pitch = find(
			(b) => b.action.type === 'deck_pitch' && b.source.ch === ch && b.source.kind === 'pitchbend'
		);
		assert.equal(pitch.length, 1);
		assert.equal(pitch[0].action.lsbOffset, null);
	}
	for (const id of [32, 33, 64, 65]) {
		const ch = id <= 33 ? (id <= 33 ? [1, 3] : [2, 4]) : [2, 4];
		for (const c of ch) {
			if ((c === 1 || c === 3) && id >= 64) continue;
			if ((c === 2 || c === 4) && id <= 33) continue;
			assert.equal(find(at(c, 'note', id)).length, 0);
		}
	}
	const browse = find(at(1, 'cc', 1))[0];
	assert.equal(browse.action.type, 'browse_encoder');
	assert.equal(browse.relative, true);
	assert.equal(browse.relativeCentered, true);
	assert.equal(browse.relativeUnit, true);
});

test('hot cues slots A-H with mirrored LEDs on all four decks', () => {
	const slots = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
	for (const [ch, deck, hotNotes] of [
		[1, 1, [34, 35, 36, 37, 38, 39, 40, 41]],
		[2, 2, [66, 67, 68, 69, 70, 71, 72, 73]],
		[3, 3, [34, 35, 36, 37, 38, 39, 40, 41]],
		[4, 4, [66, 67, 68, 69, 70, 71, 72, 73]]
	]) {
		slots.forEach((slot, i) => {
			const b = find(at(ch, 'note', hotNotes[i]));
			assert.equal(b.length, 1);
			assert.deepEqual(b[0].action, { type: 'deck_hot_cue', deck, slot });
			const led = map.leds.find(
				(l) =>
					l.trigger.kind === 'hot_cue_present' && l.trigger.deck === deck && l.trigger.slot === slot
			);
			assert.deepEqual(led.out, { ch, note: hotNotes[i], velocityOn: 0x01, velocityOff: 0x00 });
		});
	}
});

test('global master, headphones; meters absent', () => {
	assert.ok(find(at(1, 'cc', 47))[0].action.target === 'master');
	assert.ok(find(at(1, 'cc', 122))[0].action.type === 'headphone_mix');
	assert.ok(find(at(1, 'cc', 123))[0].action.type === 'headphone_level');
	assert.equal(map.meters, undefined);
	assert.equal(find(at(1, 'cc', 126)).length, 0);
	assert.equal(find(at(1, 'cc', 127)).length, 0);
	assert.ok(!map.hints.some((h) => h.source.id === 126 || h.source.id === 127));
});

test('no shift bindings; binding count 93', () => {
	assert.ok(map.bindings.every((b) => !b.shift));
	assert.ok(map.bindings.every((b) => b.action.type !== 'controller_pad_mode'));
	assert.equal(map.bindings.length, 93);
});

test('self-registers live and is flagged hardware-unconfirmed', () => {
	assert.equal(mod.DEVICE_MAP_ENTRIES.length, 1);
	assert.equal(mod.DEVICE_MAP_ENTRIES[0].key, 'BEHRINGER_CMD_STUDIO_4A_MAP');
	assert.equal(mod.DEVICE_MAP_ENTRIES[0].live, true);
	assert.equal(mod.BEHRINGER_CMD_STUDIO_4A_HARDWARE_CONFIRMED, false);
});

test('no wire is duplicated across bindings and hints', () => {
	const keys = [...map.bindings, ...map.hints].map((x) => key(x.source));
	assert.equal(new Set(keys).size, keys.length);
});
