// requirement: BEATFX-13923
// Beat FX agent parity through the real typed dispatcher (issue #9783, spec fx-fanout-20261108/01 section 4.4).
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ipc;
let uninstall;

const MIRROR_FIELDS = [
	'available',
	'target',
	'effect',
	'beats',
	'beat_ms',
	'timing',
	'level',
	'enabled',
	'quantize',
	'quantize_state',
	'tempo_bpm',
	'tempo_deck',
	'spliced',
	'release_held',
	'refusal'
];

/** Set `fx.beat_rack` on the shared flag store. */
function setRack(on) {
	ipc.buildFlags.flags = [{ flag_id: 'fx.beat_rack', enabled: on }];
}

before(async () => {
	ipc = await loadTypeScriptModule('tests/unit/fixtures/beat-fx-parity-entry.ts', {
		viteApiBase: 'http://127.0.0.1:9'
	});
	globalThis.window = {};
	uninstall = ipc.installPerformanceBrowserIpc();
});

after(() => {
	uninstall();
	delete globalThis.window;
});

test('beat_fx through the dispatcher changes the unit and the returned view shows it', async () => {
	// [if] beat_fx target 2 echo 0.75 enabled does not change the unit or the returned view [then] HTTP parity is broken, [else stop].
	setRack(true);
	const state = await ipc.dispatchPerformanceCommand({
		type: 'beat_fx',
		target: '2',
		effect: 'echo',
		beats: 0.75,
		enabled: true
	});
	assert.equal(state.beat_fx.target, '2');
	assert.equal(state.beat_fx.effect, 'echo');
	assert.equal(state.beat_fx.beats, 0.75);
	assert.equal(state.beat_fx.enabled, true);
	assert.equal(state.beat_fx.available, true);
	assert.equal(state.beat_fx.tempo_deck, '2');
	assert.deepEqual(ipc.queryPerformanceState().beat_fx, state.beat_fx);
});

test('the query mirror carries every beat_fx field and release_held follows beat_fx_release', async () => {
	// [if] the beat_fx mirror lacks a spec field or release_held ignores beat_fx_release [then] agents read stale state, [else stop].
	setRack(true);
	assert.deepEqual(
		Object.keys(ipc.queryPerformanceState().beat_fx).sort(),
		[...MIRROR_FIELDS].sort()
	);
	const held = await ipc.dispatchPerformanceCommand({
		type: 'beat_fx_release',
		held: true,
		beats: 2,
		kind: 'echo'
	});
	assert.equal(held.beat_fx.release_held, true);
	await assert.rejects(
		ipc.dispatchPerformanceCommand({ type: 'beat_fx_release', held: true }),
		/beat_fx_release is already held/
	);
	await assert.rejects(
		ipc.dispatchPerformanceCommand({ type: 'beat_fx_release', held: true, kind: 'spiral' }),
		/kind/
	);
	const let_go = await ipc.dispatchPerformanceCommand({ type: 'beat_fx_release', held: false });
	assert.equal(let_go.beat_fx.release_held, false);
	await assert.rejects(
		ipc.dispatchPerformanceCommand({ type: 'beat_fx_release', held: false }),
		/beat_fx_release is not held/
	);
});

test('with fx.beat_rack off every beat FX command is refused by the flag and nothing changes', async () => {
	// [if] a beat FX command runs with fx.beat_rack off, or its refusal does not name the flag [then] the flag gate is broken, [else stop].
	setRack(true);
	await ipc.dispatchPerformanceCommand({
		type: 'beat_fx',
		target: '1',
		level: 0.4,
		enabled: false
	});
	setRack(false);
	const before_off = ipc.queryPerformanceState().beat_fx;
	assert.equal(before_off.available, false);
	for (const command of [
		{
			type: 'beat_fx',
			target: '3',
			effect: 'delay',
			beats: 2,
			level: 0.9,
			enabled: true,
			quantize: true
		},
		{ type: 'beat_fx', xpad: 0.5 },
		{ type: 'beat_fx_release', held: true },
		{ type: 'beat_fx_release', held: false }
	]) {
		await assert.rejects(
			ipc.dispatchPerformanceCommand(command),
			/fx\.beat_rack is off/,
			command.type
		);
		assert.deepEqual(ipc.queryPerformanceState().beat_fx, before_off, JSON.stringify(command));
	}
});
