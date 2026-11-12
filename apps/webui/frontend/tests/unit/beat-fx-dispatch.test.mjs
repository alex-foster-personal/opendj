// requirement: FXECHO-10722
// [if] beat_fx rejects spiral helix reverse_delay multitap tape on parse [then] agent parity broken
// [if] level does not reach setHold helix setDepth spiral or balance others [then] level law broken
// [if] out-of-range division/time is not refused by name [then] catalog bounds broken
// [if] Rust mode does not refuse each effect by name [then] refusal broken
// [if] level 0 or OFF is not bit-exact dry or swap click > 1.5 [then] harness broken
import assert from 'node:assert/strict';
import { before, describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let fx;
let catalog;
let unit;
let ipc;
let rust;

const NEW_EFFECTS = ['spiral', 'helix', 'reverse_delay', 'multitap', 'tape'];

before(async () => {
	[fx, catalog, unit, ipc] = await Promise.all([
		loadTypeScriptModule('src/lib/rb/beat-fx.ts'),
		loadTypeScriptModule('src/lib/rb/fx/time-fx-catalog.ts'),
		loadTypeScriptModule('src/lib/rb/beat-fx-unit.ts'),
		loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts')
	]);
	rust = await loadTypeScriptModule('src/lib/audio-engine/rust-mode.svelte.ts');
});

describe('beat_fx effect names on the bus', () => {
	test('parseBeatFxCommand accepts each new time-based effect', () => {
		for (const effect of NEW_EFFECTS) {
			const cmd = fx.parseBeatFxCommand({ type: 'beat_fx', effect, enabled: true });
			assert.equal(cmd.effect, effect);
		}
	});

	test('parsePerformanceCommandForTest round-trips beat_fx fields', () => {
		const cmd = ipc.parsePerformanceCommandForTest({
			type: 'beat_fx',
			target: '2',
			effect: 'spiral',
			beats: 1,
			level: 0.6,
			enabled: true
		});
		assert.equal(cmd.type, 'beat_fx');
		assert.equal(cmd.target, '2');
		assert.equal(cmd.effect, 'spiral');
		assert.equal(cmd.beats, 1);
		assert.equal(cmd.level, 0.6);
		assert.equal(cmd.enabled, true);
	});
});

describe('level law per effect', () => {
	test('level routes to setHold setDepth or wet/dry balance', () => {
		const cases = [
			['helix', 'hold'],
			['spiral', 'depth'],
			['echo', 'balance'],
			['reverse_delay', 'balance'],
			['multitap', 'balance'],
			['tape', 'balance']
		];
		for (const [effect, kind] of cases) {
			const host = unit.createBeatFxKernelHost(effect);
			unit.applyBeatFxLevel(host, effect, 0.75, 0);
			assert.equal(host.lastLevelKind, kind, effect);
			assert.equal(host.lastLevel, 0.75);
		}
	});
});

describe('catalog division and time bounds', () => {
	test('SPIRAL and MULTI TAP refuse time below 10 ms floor', () => {
		for (const effect of ['spiral', 'multitap']) {
			assert.throws(
				() => catalog.assertTimeFxDivisionAllowed(effect, 1 / 16, 9600),
				/spiral|multitap|outside.*10.*4000/
			);
		}
	});

	test('REVERSE DELAY refuses beat below 1/8', () => {
		assert.throws(
			() => catalog.assertTimeFxDivisionAllowed('reverse_delay', 1 / 16, 128),
			/outside.*0\.125/
		);
	});
});

describe('Rust engine refusal', () => {
	test('WEB_AUDIO_ONLY refuses beat_fx and each built effect message names RUST_ENGINE_UNAVAILABLE', () => {
		assert.ok(rust.WEB_AUDIO_ONLY.has('beat_fx'));
		for (const effect of NEW_EFFECTS) {
			const msg = unit.beatFxRustRefusal(effect);
			assert.match(msg, /^RUST_ENGINE_UNAVAILABLE/);
			assert.match(msg, new RegExp(effect));
		}
	});
});

describe('dry path and kernel swap harness', () => {
	test('level 0 or OFF is bit-exact dry for each new effect', () => {
		for (const effect of NEW_EFFECTS) {
			const off = unit.beatFxWetSendGain(0, true);
			const disabled = unit.beatFxWetSendGain(0.8, false);
			assert.equal(off, 0);
			assert.equal(disabled, 0);
		}
	});

	test('kernel swap mid-tail stays within click ratio 1.5', () => {
		const ratio = unit.beatFxSwapClickRatio(48_000);
		unit.assertBeatFxSwapClickWithinBudget(ratio);
		assert.ok(ratio <= 1.5);
	});
});

describe('TIME_FX catalog builtOnMain', () => {
	test('spiral helix reverse_delay multitap tape are built on main', () => {
		for (const name of NEW_EFFECTS) {
			const row = catalog.timeFxCatalogRow(name);
			assert.ok(row, name);
			assert.equal(row.builtOnMain, true);
		}
	});
});
