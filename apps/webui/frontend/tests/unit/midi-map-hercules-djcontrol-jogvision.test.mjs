// Unit tests for the Hercules DJControl Jogvision device map
// (src/lib/rb/midi/maps/hercules-djcontrol-jogvision.ts).
//
// Regression lines:
//   if registerDeviceMap(HERCULES_DJCONTROL_JOGVISION_MAP) throws then broken
//   if a binding or hint cites a wire absent from the [HC] chart then broken
//   if play/cue/sync/fader/jog/crossfader are missing then broken
//   if FX rows bind instead of hint then broken (P0 has no beat-FX; no filter knob)
//   if nameMatch rejects DJControl Jogvision or matches DDJ-FLX4 then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let vite;
let webmidi;
let mapModule;
let registry;

/** Independent transcription of [HC] MIDI input rows (issue #10848 plan). */
function buildChart() {
	const set = new Set();
	const add = (ch, kind, id) => set.add(`${ch}:${kind}:0x${id.toString(16)}`);
	const noteRange = (ch, from, to) => {
		for (let id = from; id <= to; id++) add(ch, 'note', id);
	};
	const ccRange = (ch, from, to) => {
		for (let id = from; id <= to; id++) add(ch, 'cc', id);
	};
	for (const ch of [1, 2]) {
		noteRange(ch, 0x00, 0x06);
		noteRange(ch, 0x08, 0x0f);
		noteRange(ch, 0x10, 0x17);
		noteRange(ch, 0x18, 0x1f);
		noteRange(ch, 0x20, 0x2f);
		noteRange(ch, 0x30, 0x39);
		for (const id of [0x50, 0x51, 0x52, 0x53, 0x54, 0x55, 0x56, 0x57]) add(ch, 'note', id);
		ccRange(ch, 0x01, 0x05);
		add(ch, 'cc', 0x0f);
		ccRange(ch, 0x21, 0x25);
		ccRange(ch, 0x40, 0x50);
	}
	for (const id of [0x45, 0x46, 0x47, 0x48, 0x49, 0x4a, 0x4b, 0x4c, 0x4d, 0x4e, 0x4f]) {
		add(1, 'note', id);
	}
	for (const id of [0x00, 0x20, 0x4e, 0x51]) add(1, 'cc', id);
	return set;
}

const CHART = buildChart();

function sourceKey({ ch, kind, id }) {
	return `${ch}:${kind}:0x${id.toString(16)}`;
}

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
	mapModule = await vite.ssrLoadModule('/src/lib/rb/midi/maps/hercules-djcontrol-jogvision.ts');
	registry = await vite.ssrLoadModule('/src/lib/rb/midi/maps/index.ts');
});

after(async () => {
	await vite.close();
});

function map() {
	return mapModule.HERCULES_DJCONTROL_JOGVISION_MAP;
}

function bindings(pred) {
	return map().bindings.filter(pred);
}

test('map registers cleanly through the core fail-fast validator', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(map());
	webmidi._resetMidiForTests();
});

test('every binding and hint source is in the chart allowlist', () => {
	const bound = new Set();
	const hinted = new Set();
	for (const b of map().bindings) {
		const key = sourceKey(b.source);
		assert.ok(CHART.has(key), `binding not in chart: ${key}`);
		assert.ok(!hinted.has(key), `binding collides with hint: ${key}`);
		assert.ok(!bound.has(key), `duplicate binding: ${key}`);
		bound.add(key);
	}
	for (const h of map().hints ?? []) {
		const key = sourceKey(h.source);
		assert.ok(CHART.has(key), `hint not in chart: ${key}`);
		assert.ok(!bound.has(key), `hint collides with binding: ${key}`);
		assert.ok(!hinted.has(key), `duplicate hint: ${key}`);
		hinted.add(key);
	}
});

test('nameMatch covers Jogvision ports and rejects siblings', () => {
	const re = new RegExp(map().nameMatch, 'i');
	assert.ok(re.test('DJControl Jogvision'));
	assert.ok(re.test('DJControl Jogvision MIDI 1'));
	assert.ok(re.test('Hercules DJControl Jogvision'));
	assert.ok(!re.test('DJControl Inpulse 500'));
	assert.ok(!re.test('DJControl Instinct'));
	assert.ok(!re.test('DDJ-FLX4'));
	assert.ok(!/\(\?/.test(map().nameMatch), 'nameMatch must not use lookaround');
});

test('transport, fader, jog and crossfader on cited wires', () => {
	for (const [ch, deck] of [
		[1, 1],
		[2, 2]
	]) {
		assert.ok(
			bindings(
				(b) =>
					b.action.type === 'deck_play_toggle' &&
					b.action.deck === deck &&
					b.source.id === 0x32 &&
					b.source.ch === ch
			).length === 1
		);
		assert.ok(
			bindings(
				(b) =>
					b.action.type === 'deck_cue' &&
					b.action.deck === deck &&
					b.source.id === 0x31 &&
					b.source.ch === ch
			).length === 1
		);
		assert.ok(
			bindings(
				(b) =>
					b.action.type === 'deck_sync_toggle' &&
					b.action.deck === deck &&
					b.source.id === 0x30 &&
					b.source.ch === ch
			).length === 1
		);
		assert.ok(
			bindings(
				(b) =>
					b.action.type === 'mixer_channel' &&
					b.action.deck === deck &&
					b.action.target === 'fader' &&
					b.source.kind === 'cc' &&
					b.source.id === 0x01
			).length === 1
		);
		for (const jogId of [0x40, 0x41]) {
			const j = bindings(
				(b) =>
					b.action.type === 'deck_jog' &&
					b.action.deck === deck &&
					b.source.id === jogId &&
					b.source.ch === ch
			);
			assert.equal(j.length, 1);
			assert.equal(j[0].relative, true);
			assert.notEqual(j[0].relativeCentered, true);
		}
	}
	assert.ok(
		bindings(
			(b) =>
				b.action.type === 'mixer_global' &&
				b.action.target === 'crossfader' &&
				b.source.ch === 1 &&
				b.source.id === 0x00
		).length === 1
	);
});

test('pitch is 14-bit deck_pitch with lsbOffset 32 on both decks', () => {
	for (const deck of [1, 2]) {
		const p = bindings((b) => b.action.type === 'deck_pitch' && b.action.deck === deck);
		assert.equal(p.length, 1);
		assert.equal(p[0].source.id, 0x02);
		assert.equal(p[0].action.lsbOffset, 0x20);
	}
});

test('FX chart rows are hints only (P0 has no beat-FX; no absolute filter knob)', () => {
	const fxNotes = [0x03, 0x04, 0x05, 0x06, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f, 0x50, 0x52, 0x54];
	const fxCc = [0x0f, 0x46, 0x47, 0x4c, 0x4d];
	for (const ch of [1, 2]) {
		for (const id of fxNotes) {
			assert.equal(
				bindings((b) => b.source.ch === ch && b.source.kind === 'note' && b.source.id === id)
					.length,
				0,
				`FX note 0x${id.toString(16)} ch${ch} must not bind`
			);
			assert.ok(
				(map().hints ?? []).some(
					(h) => h.source.ch === ch && h.source.kind === 'note' && h.source.id === id
				),
				`FX note 0x${id.toString(16)} ch${ch} must hint`
			);
		}
		for (const id of fxCc) {
			assert.equal(
				bindings((b) => b.source.ch === ch && b.source.kind === 'cc' && b.source.id === id).length,
				0,
				`FX cc 0x${id.toString(16)} ch${ch} must not bind`
			);
			assert.ok(
				(map().hints ?? []).some(
					(h) => h.source.ch === ch && h.source.kind === 'cc' && h.source.id === id
				),
				`FX cc 0x${id.toString(16)} ch${ch} must hint`
			);
		}
	}
});

test('hot-cue, play, sync and listen LEDs use 0x7f / 0x00', () => {
	for (const deck of [1, 2]) {
		const ch = deck;
		for (const [trigger, note] of [
			['deck_playing', 0x32],
			['beat_sync_enabled', 0x30],
			['channel_cue_enabled', 0x33]
		]) {
			const rule = map().leds.find((r) => r.trigger.kind === trigger && r.trigger.deck === deck);
			assert.ok(rule, `${trigger} deck ${deck}`);
			assert.equal(rule.out.ch, ch);
			assert.equal(rule.out.note, note);
			assert.equal(rule.out.velocityOn, 0x7f);
			assert.equal(rule.out.velocityOff, 0x00);
		}
		for (let i = 0; i < 8; i++) {
			const slot = 'ABCDEFGH'[i];
			const rule = map().leds.find(
				(r) =>
					r.trigger.kind === 'hot_cue_present' && r.trigger.deck === deck && r.trigger.slot === slot
			);
			assert.ok(rule, `hot cue ${slot} deck ${deck}`);
			assert.equal(rule.out.note, 0x10 + i);
			assert.equal(rule.out.velocityOn, 0x7f);
			assert.equal(rule.out.velocityOff, 0x00);
		}
	}
});

test('DEVICE_MAP_ENTRIES and registry expose live map', () => {
	assert.equal(mapModule.DEVICE_MAP_ENTRIES.length, 1);
	const entry = mapModule.DEVICE_MAP_ENTRIES[0];
	assert.equal(entry.key, 'HERCULES_DJCONTROL_JOGVISION_MAP');
	assert.equal(entry.live, true);
	assert.ok(
		registry.DEVICE_MAP_REGISTRY.some(
			(m) => m.vendor === 'Hercules' && m.nameMatch === map().nameMatch
		)
	);
});

test('hardware_confirmed export is false', () => {
	assert.equal(mapModule.HERCULES_DJCONTROL_JOGVISION_HARDWARE_CONFIRMED, false);
});
