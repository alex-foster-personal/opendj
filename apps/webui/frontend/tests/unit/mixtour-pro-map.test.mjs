// Reloop Mixtour Pro map acceptance at the production map/registry boundary.
//
//   if a Pro port resolves to the classic map then AC1 is broken
//   if deck d does not use N=d and P=4+d then four-deck routing is broken
//   if Pro SYNC/LOOP/SHIFT+PLAY/SHIFT+CUE resolve as legacy actions then broken
//   if pads 1..8 or their SHIFT bank are missing on any deck then broken
//   if transport colors or VU CC 0x1F drift from the captured protocol then broken

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let vite;
let webmidi;
let pro;
let classic;

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true },
		plugins: [svelte()],
		resolve: { alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') }, conditions: ['browser'] }
	});
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	pro = await vite.ssrLoadModule('/src/lib/rb/midi/maps/reloop-mixtour-pro.ts');
	classic = await vite.ssrLoadModule('/src/lib/rb/midi/maps/reloop-mixtour.ts');
});

after(async () => {
	await vite.close();
});

function binding(ch, kind, id) {
	return pro.RELOOP_MIXTOUR_PRO_MAP.bindings.find(
		(candidate) => candidate.source.ch === ch && candidate.source.kind === kind && candidate.source.id === id
	);
}

test('classic and Pro identities are mutually exclusive and resolver-safe', () => {
	const proName = 'Reloop Mixtour Pro';
	assert.equal(new RegExp(pro.RELOOP_MIXTOUR_PRO_MAP.nameMatch, 'i').test(proName), true);
	assert.equal(new RegExp(classic.RELOOP_MIXTOUR_MAP.nameMatch, 'i').test(proName), false);
	assert.equal(new RegExp(pro.RELOOP_MIXTOUR_PRO_MAP.nameMatch, 'i').test('Reloop Mixtour'), false);

	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(classic.RELOOP_MIXTOUR_MAP);
	webmidi.registerDeviceMap(pro.RELOOP_MIXTOUR_PRO_MAP);
	assert.equal(webmidi.resolveMapForPort(proName)?.nameMatch, pro.RELOOP_MIXTOUR_PRO_MAP.nameMatch);
	assert.equal(webmidi.resolveMapForPort('Reloop Mixtour')?.nameMatch, classic.RELOOP_MIXTOUR_MAP.nameMatch);
	webmidi._resetMidiForTests();
});

test('map registers cleanly and every deck follows the N/P channel families', () => {
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(pro.RELOOP_MIXTOUR_PRO_MAP);
	for (let deck = 1; deck <= 4; deck += 1) {
		assert.deepEqual(binding(deck, 'note', 0x00).action, { type: 'deck_play_toggle', deck });
		assert.deepEqual(binding(deck, 'note', 0x01).action, { type: 'deck_cue', deck });
		assert.deepEqual(binding(deck, 'note', 0x02).action, { type: 'deck_sync_toggle', deck });
		assert.deepEqual(binding(deck, 'note', 0x03).action, { type: 'deck_manual_loop_cycle', deck });
		assert.deepEqual(binding(4 + deck, 'note', 0x0a).action, { type: 'browse_load', deck });
		assert.deepEqual(binding(4 + deck, 'note', 0x24).action, { type: 'deck_stem_eq_toggle', deck });
		const neuralLed = pro.RELOOP_MIXTOUR_PRO_MAP.leds.find((rule) =>
			rule.trigger.kind === 'stem_eq_enabled' && rule.trigger.deck === deck);
		assert.deepEqual(neuralLed.out, { ch: 4 + deck, note: 0x24, velocityOn: 127, velocityOff: 1 });
	}
	webmidi._resetMidiForTests();
});

test('known legacy-collision bytes have only their captured Pro meanings', () => {
	for (let deck = 1; deck <= 4; deck += 1) {
		assert.equal(binding(deck, 'note', 0x02).action.type, 'deck_sync_toggle');
		assert.equal(binding(deck, 'note', 0x03).action.type, 'deck_manual_loop_cycle');
		assert.equal(binding(deck, 'note', 0x0b), undefined, 'SHIFT+PLAY pitch bend stays inert until exact hold semantics land');
		assert.equal(binding(deck, 'note', 0x0c), undefined, 'SHIFT+CUE pitch bend stays inert until exact hold semantics land');
	}
});

test('mixer strip uses captured CC 0x16..0x1C on all four N channels', () => {
	for (let deck = 1; deck <= 4; deck += 1) {
		const expected = [
			[0x16, 'trim', undefined],
			[0x17, 'eq', 'high'],
			[0x18, 'eq', 'mid'],
			[0x19, 'eq', 'low'],
			[0x1a, 'filter', undefined],
			[0x1c, 'fader', undefined]
		];
		for (const [id, target, band] of expected) {
			const action = binding(deck, 'cc', id).action;
			assert.equal(action.type, 'mixer_channel');
			assert.equal(action.deck, deck);
			assert.equal(action.target, target);
			assert.equal(action.band, band);
		}
	}
});

test('all eight pad modes, pads and SHIFT pads exist on every P channel', () => {
	for (let deck = 1; deck <= 4; deck += 1) {
		const p = 4 + deck;
		for (let index = 0; index < 8; index += 1) {
			assert.equal(binding(p, 'note', index).action.type, 'controller_pad_mode');
			assert.deepEqual(binding(p, 'note', 0x14 + index).action, {
				type: 'controller_pad', deck, pad: index + 1, shifted: false
			});
			assert.deepEqual(binding(p, 'note', 0x1c + index).action, {
				type: 'controller_pad', deck, pad: index + 1, shifted: true
			});
		}
	}
});

test('global controls and browse detent normalization match the corrected map', () => {
	const browse = binding(16, 'cc', 0x00);
	assert.equal(browse.action.type, 'browse_encoder');
	assert.equal(browse.relative, true);
	assert.equal(browse.relativeUnit, true);
	assert.equal(browse.invert, true);
	assert.equal(binding(16, 'cc', 0x08).action.target, 'crossfader');
	assert.equal(binding(16, 'cc', 0x0a).action.target, 'master');
	assert.equal(binding(16, 'cc', 0x0c).action.type, 'headphone_level');
	assert.equal(binding(16, 'cc', 0x0d).action.type, 'headphone_mix');
});

test('every documented input address is either bound or explicitly identified', () => {
	const protocol = JSON.parse(
		readFileSync(
			resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/reloop-mixtour-pro/midi.json'),
			'utf8'
		)
	);
	const bindings = new Set(
		pro.RELOOP_MIXTOUR_PRO_MAP.bindings.map(({ source }) => `${source.ch}:${source.kind}:${source.id}`)
	);
	const hints = new Set(
		pro.RELOOP_MIXTOUR_PRO_MAP.hints.map(({ source }) => `${source.ch}:${source.kind}:${source.id}`)
	);
	const channels = {
		G: [16],
		N: [1, 2, 3, 4],
		P: [5, 6, 7, 8],
		E: [9, 10, 11, 12]
	};
	for (const control of protocol.controls.filter(({ direction }) => direction === 'in')) {
		for (const ch of channels[control.ch_key]) {
			const key = `${ch}:${control.type}:${Number.parseInt(control.hex, 16)}`;
			assert.equal(
				bindings.has(key) || hints.has(key),
				true,
				`${control.name} (${key}) is neither usable nor visibly identified`
			);
		}
	}
});

test('feedback carries deck colors, RGB hot cues, mode state and discrete VU addresses', () => {
	for (let deck = 1; deck <= 4; deck += 1) {
		const play = pro.RELOOP_MIXTOUR_PRO_MAP.leds.find(
			(rule) => rule.trigger.kind === 'deck_playing' && rule.trigger.deck === deck
		);
		assert.equal(play.out.ch, deck);
		assert.equal(play.out.note, 0x00);
		assert.equal(play.out.velocityOn, deck <= 2 ? 0x7e : 0x7d);
		assert.equal(play.out.velocityOff, deck <= 2 ? 0x02 : 0x01);
		assert.equal(
			pro.RELOOP_MIXTOUR_PRO_MAP.leds.filter(
				(rule) => rule.trigger.kind === 'pad_mode_selected' && rule.trigger.deck === deck
			).length,
			8
		);
		assert.equal(
			pro.RELOOP_MIXTOUR_PRO_MAP.leds.filter(
				(rule) => rule.trigger.kind === 'hot_cue_present' && rule.trigger.deck === deck
			).length,
			16
		);
		assert.deepEqual(pro.MIXTOUR_PRO_METERS[deck - 1], {
			deck,
			out: { ch: deck, cc: 0x1f, maxValue: 6 }
		});
	}
});
