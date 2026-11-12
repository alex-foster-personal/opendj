// STEMPERF-11663: per-stem FX send (a send gain per part into the channel's
// colour FX input), the stem_fx_send command and its ramp.
//
// [if] setStemFxSend('vocal', 0.7) runs [then] only the vocal send ramps
//   (setTargetAtTime with the 0.01 constant) ⛔️ if a part gain moves, the dry
//   mix changes when the DJ only meant to feed the FX.
// [if] stem_fx_send names drums on a roformer2 deck [then] it throws the named
//   "has no drums control" error ⛔️ if it silently stored a dead control.
// [if] the deck is empty [then] stem_fx_send is a silent no-op like stem_mute.
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

globalThis.location ??= { search: '' };

const fx = await loadTypeScriptModule('tests/unit/fixtures/stem-fx-send-entry.ts');
const orders = await loadTypeScriptModule('tests/unit/fixtures/agent-orders-entry.ts');

afterEach(() => {
	delete globalThis.window;
});

function fakeParam(value) {
	return {
		value,
		calls: [],
		setTargetAtTime(v, t, c) {
			this.calls.push([v, t, c]);
			this.value = v;
		}
	};
}

function fakeNode(name, value = 1) {
	return {
		name,
		gain: fakeParam(value),
		connections: [],
		disconnects: 0,
		connect(node) {
			this.connections.push(node);
		},
		disconnect() {
			this.disconnects += 1;
		}
	};
}

function fakeContext() {
	let n = 0;
	return { currentTime: 0, createGain: () => fakeNode(`gain${n++}`) };
}

function processor(layout = 'demucs4') {
	const parts =
		layout === 'roformer2' ? ['vocals', 'instrumental'] : ['vocals', 'drums', 'bass', 'other'];
	const ctx = fakeContext();
	const procs = {};
	const gains = {};
	const sends = {};
	for (const part of parts) {
		procs[part] = { connect() {}, disconnect() {}, dispose: async () => {} };
		const bus = fx.createStemPartBus(ctx, procs[part]);
		gains[part] = bus.gain;
		sends[part] = bus.send;
	}
	const p = new fx.AlignedStemDeckProcessor(ctx, procs, gains, sends, 0, layout, {});
	return { p, gains, sends, parts };
}

function readyDeck(layout, processorValue) {
	const available =
		layout === 'roformer2'
			? ['vocal', 'instrumental']
			: ['vocal', 'instrumental', 'drums', 'bass', 'other'];
	const st = {
		stems: {
			status: 'ready',
			layout,
			available_controls: available,
			controls: fx.createDefaultStemControls(),
			error: null
		}
	};
	const rt = { processor: processorValue };
	return {
		st,
		deps: {
			requireLoaded: () => ({ st, rt }),
			getChannel: () => ({}),
			heldStems: () => null,
			engageHeldStems: () => {
				throw new Error('no held bundle');
			}
		}
	};
}

test('setStemFxSend ramps only the vocal send and no part gain', () => {
	const { p, gains, sends, parts } = processor();
	p.setStemFxSend('vocal', 0.7);
	assert.deepEqual(sends.vocals.gain.calls, [[0.7, 0, 0.01]]);
	for (const part of parts) {
		assert.deepEqual(gains[part].gain.calls, [], `part gain ${part} moved`);
		if (part !== 'vocals') assert.deepEqual(sends[part].gain.calls, [], `send ${part} moved`);
	}
	assert.equal(p.controls().vocal.fx_send, 0.7);
});

test('mute and solo gate the part gain only; the dry sum ignores fx_send', () => {
	const { p, gains } = processor();
	p.setStemFxSend('vocal', 0.7);
	const controls = p.controls();
	controls.vocal.muted = true;
	p.setControls(controls);
	assert.equal(gains.vocals.gain.calls.at(-1)[0], 0);
	const dry = fx.createDefaultStemControls();
	const wet = fx.createDefaultStemControls();
	for (const stem of Object.keys(wet)) wet[stem].fx_send = 0.9;
	assert.deepEqual(fx.stemPartGains(wet, 'demucs4'), fx.stemPartGains(dry, 'demucs4'));
	assert.equal(fx.stemPartSends(controls, 'demucs4').vocals, 0.7);
});

test('stem_fx_send for drums on a roformer2 deck throws the named control error', () => {
	const { p } = processor('roformer2');
	const { deps } = readyDeck('roformer2', p);
	assert.throws(
		() => fx.applyStemControl(1, 'drums', 'fx_send', 0.5, deps),
		/has no drums control/
	);
});

test('stem_fx_send on an empty deck is a silent no-op', () => {
	const deps = {
		requireLoaded: (_deck, op) => {
			throw new Error(`${op}: no track loaded on deck 1`);
		},
		getChannel: () => ({}),
		heldStems: () => null,
		engageHeldStems: () => {}
	};
	assert.doesNotThrow(() => fx.applyStemControl(1, 'vocal', 'fx_send', 0.7, deps));
});

test('stem_fx_send stores controls.vocal.fx_send for the mirror and moves no part gain', () => {
	const { p, gains, sends } = processor();
	const { st, deps } = readyDeck('demucs4', p);
	fx.applyStemControl(1, 'vocal', 'fx_send', 0.7, deps);
	assert.equal(st.stems.controls.vocal.fx_send, 0.7);
	assert.deepEqual(sends.vocals.gain.calls, [[0.7, 0, 0.01]]);
	for (const node of Object.values(gains)) assert.deepEqual(node.gain.calls, []);
});

test('the command parser accepts stem_fx_send and refuses bad values and keys', () => {
	const command = { type: 'stem_fx_send', deck: 1, stem: 'vocal', value: 0.7 };
	assert.deepEqual(fx._parseCommand(command), command);
	assert.throws(() => fx._parseCommand({ ...command, value: 1.2 }));
	assert.throws(() => fx._parseCommand({ ...command, extra: 1 }));
});

test('a stem_fx_send ramp reads its start from the deck state and is accepted', async () => {
	const state = {
		decks: {
			1: { stems: { controls: { vocal: { muted: false, solo: false, gain: 0.5, fx_send: 0.3 } } } }
		}
	};
	const command = { type: 'stem_fx_send', deck: 1, stem: 'vocal', value: 0 };
	assert.equal(orders.rampStartValue(state, command), 0.3);
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const uninstall = orders.installPerformanceBrowserIpc();
	try {
		const result = await orders.executeAgentOrder({
			kind: 'ramp',
			payload: { command, to: 0.5, over: { unit: 'ms', n: 20, clock: 1 } }
		});
		assert.doesNotMatch(JSON.stringify(result), /ramp command must be/);
	} finally {
		uninstall();
	}
});
