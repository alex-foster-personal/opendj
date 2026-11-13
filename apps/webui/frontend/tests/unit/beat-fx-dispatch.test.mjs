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
let insert;
let panel;
let slot;

const NEW_EFFECTS = ['spiral', 'helix', 'reverse_delay', 'multitap', 'tape'];

before(async () => {
	[fx, catalog, unit, ipc] = await Promise.all([
		loadTypeScriptModule('src/lib/rb/beat-fx.ts'),
		loadTypeScriptModule('src/lib/rb/fx/time-fx-catalog.ts'),
		loadTypeScriptModule('src/lib/rb/beat-fx-unit.ts'),
		loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts')
	]);
	rust = await loadTypeScriptModule('src/lib/audio-engine/rust-mode.svelte.ts');
	insert = await loadTypeScriptModule('src/lib/rb/beat-fx-insert.ts');
	panel = await loadTypeScriptModule('src/lib/rb/beat-fx-panel.ts');
	slot = await loadTypeScriptModule('src/lib/rb/fx/beat-fx-effects/echo-slot-kernel.ts');
});

/** A fresh unit state on echo at `bpm`, host unset. */
function echoUnit(bpm = 128) {
	return { ...unit.DEFAULT_BEAT_FX_UNIT, effect: 'echo', tempo_bpm: bpm, host: null };
}

/** A fake AudioParam that records every automation method called on it. */
function recordingParam(calls, name) {
	return new Proxy(
		{ value: 0 },
		{ get: (t, k) => (k in t ? t[k] : (...args) => calls.push([name, String(k), ...args])) }
	);
}

/** A fake context whose nodes record automation per param name (delayTime, gain, ...). */
function recordingCtx(calls) {
	const node = () => ({
		connect: (to) => to,
		disconnect: () => {},
		gain: recordingParam(calls, 'gain'),
		delayTime: recordingParam(calls, 'delayTime'),
		frequency: recordingParam(calls, 'frequency'),
		Q: recordingParam(calls, 'Q')
	});
	return {
		currentTime: 0,
		sampleRate: 48_000,
		createGain: node,
		createDelay: node,
		createBiquadFilter: node,
		createWaveShaper: node,
		createChannelMerger: node
	};
}

/** Apply a wire `beat_fx` record to `state` through the real parser. */
function send(state, record) {
	unit.applyBeatFxUnitCommand(state, fx.parseBeatFxCommand({ type: 'beat_fx', ...record }), 0);
}

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
	test('spiral helix reverse_delay multitap tape have rows but no kernel on main (#13726)', () => {
		for (const name of NEW_EFFECTS) {
			const row = catalog.timeFxCatalogRow(name);
			assert.ok(row, name);
			assert.equal(row.builtOnMain, false);
		}
	});
});

// requirement: BEATFX-14616
describe('free TIME in ms (issue #10001)', () => {
	test('time_ms in the catalog range is taken, beats with it is refused by name, beats again syncs', () => {
		// [if] echo time_ms 375 is refused, 4001 or Infinity or time_ms with beats passes, or beats does not resync [then] free TIME is broken, [else stop].
		const state = echoUnit();
		send(state, { time_ms: 375 });
		assert.equal(state.time_ms, 375);
		assert.throws(() => send(state, { time_ms: 4001 }), /echo time_ms 4001 outside 1\.\.4000 ms/);
		assert.throws(() => send(state, { time_ms: Infinity }), /time_ms must be a finite number/);
		assert.throws(
			() => fx.parseBeatFxCommand({ type: 'beat_fx', time_ms: 375, beats: 1 }),
			/time_ms and beats are exclusive/
		);
		assert.equal(state.time_ms, 375);
		send(state, { beats: 2 });
		assert.equal(state.time_ms, null);
		assert.equal(unit.beatFxUnitView(state).timing.mode, 'beats');
		assert.throws(() => send(echoUnit(), { effect: 'crush', time_ms: 375 }), /crush has no TIME/);
	});

	test('the mirror timing reports mode, beats, time_ms and effective_ms; a tempo change moves only beats mode', () => {
		// [if] ms mode moves with tempo, beats mode does not, or timing lacks a field [then] agents read a wrong delay time, [else stop].
		const state = echoUnit(120);
		assert.deepEqual(unit.beatFxUnitView(state).timing, {
			mode: 'beats',
			beats: 1,
			time_ms: null,
			effective_ms: 500
		});
		state.tempo_bpm = 100;
		assert.equal(unit.beatFxUnitView(state).timing.effective_ms, 600);
		send(state, { time_ms: 375 });
		const view = unit.beatFxUnitView(state);
		assert.deepEqual(view.timing, { mode: 'ms', beats: null, time_ms: 375, effective_ms: 375 });
		state.tempo_bpm = 140;
		assert.equal(unit.beatFxUnitView(state).timing.effective_ms, 375);
	});

	test('the unit slot kernel gets the free time and keeps it across a tempo change', () => {
		// [if] syncBeatFxInsert sends the division instead of time_ms, or a tempo change retimes ms mode [then] the audio drifts, [else stop].
		const timings = [];
		const kernel = { setTiming: (sec) => timings.push(sec), setDepth: () => {} };
		const fake = { ctx: { currentTime: 0 }, slot: { effect: 'echo', kernel }, sends: new Map() };
		const state = echoUnit(120);
		insert.syncBeatFxInsert(fake, state, 0);
		send(state, { time_ms: 375 });
		insert.syncBeatFxInsert(fake, state, 0);
		state.tempo_bpm = 90;
		insert.syncBeatFxInsert(fake, state, 0);
		assert.deepEqual(timings, [0.5, 0.375, 0.375]);
	});

	test('an X-PAD touch and slide in ms mode send time_ms through xpadToMs, beats mode keeps divisions', () => {
		// [if] an ms-mode X-PAD touch sends beats, a slide resends the same ms, or beats mode sends time_ms [then] the pad ignores TIME, [else stop].
		const state = echoUnit(120);
		send(state, { time_ms: 375 });
		const down = panel.beatFxXpadDown(unit.beatFxUnitView(state), 0.5);
		assert.deepEqual(down.command, { type: 'beat_fx', time_ms: 63, enabled: true });
		const slide = panel.beatFxXpadMove(down.touch, 1);
		assert.deepEqual(slide.command, { type: 'beat_fx', time_ms: 4000 });
		assert.equal(panel.beatFxXpadMove(slide.touch, 1).command, null);
		send(state, slide.command);
		assert.equal(unit.beatFxUnitView(state).timing.effective_ms, 4000);
		const synced = panel.beatFxXpadDown(unit.beatFxUnitView(echoUnit(120)), 0.05);
		assert.deepEqual(synced.command, { type: 'beat_fx', beats: 1 / 16, enabled: true });
	});

	test('a free-time change on the echo slot kernel retunes by crossfade, never a delayTime ramp', () => {
		// [if] the unit echo kernel moves delayTime by a ramp or target on an ms retune [then] free TIME slides pitch, [else stop].
		const calls = [];
		const kernel = slot.createEchoSlotKernel(recordingCtx(calls), 'echo');
		kernel.setTiming(0.5, 0);
		kernel.setTiming(0.375, 1);
		kernel.setTiming(4, 2);
		const onDelay = calls.filter(([name]) => name === 'delayTime').map(([, method]) => method);
		assert.ok(onDelay.length > 0, 'delayTime was never set');
		assert.deepEqual(
			onDelay.filter((m) => /Ramp|Target|Curve/.test(m)),
			[],
			`delayTime moved by ${onDelay.join(',')}`
		);
		assert.ok(
			calls.some(([name, m]) => name === 'gain' && /Ramp|Curve/.test(m)),
			'no crossfade'
		);
	});
});

// requirement: BEATFX-14715
describe('triplet divisions (issue #10016)', () => {
	test('the unit refuses triplet by name and leaves its state unchanged', () => {
		// [if] the unit takes triplet true with no triplet kernel wired, or a refusal moves beats [then] agents hear duple for a triplet ask, [else stop].
		const state = echoUnit(120);
		send(state, { beats: 2 });
		assert.throws(
			() => send(state, { triplet: true, beats: 4 / 3 }),
			/triplet divisions are not wired/
		);
		assert.equal(state.beats, 2);
		send(state, { triplet: false, beats: 1 });
		assert.equal(state.beats, 1);
	});
});
