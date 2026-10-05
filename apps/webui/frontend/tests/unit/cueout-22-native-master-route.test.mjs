// requirement: CUEOUT-22 (packaged app MAIN pin vs calibration)
// [if] the native cue sink is available and a MAIN device is selected [then] routes.master.state is selected and calibration may start
// [if] the master route is unsupported or failed while a MAIN id is selected [then] calibration still refuses
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const MAIN_ID = 'native:BuiltInSpeakerDevice';
const CUE_ID = 'native:Headphones';

class ReplyWorker {
	constructor() {
		this.onmessage = null;
		this.onerror = null;
		this.failNext = false;
	}
	postMessage(message) {
		queueMicrotask(() => {
			if (this.onmessage === null) return;
			if (message.kind === 'connect') {
				this.onmessage({ data: { kind: 'open' } });
				return;
			}
			if (message.kind !== 'command') return;
			if (this.failNext) {
				this.failNext = false;
				this.onmessage({
					data: {
						kind: 'message',
						payload: { type: 'error', id: message.payload.id, message: 'set_master refused' }
					}
				});
				return;
			}
			this.onmessage({
				data: { kind: 'message', payload: { type: 'ok', id: message.payload.id } }
			});
		});
	}
	terminate() {}
}

function fakeAudioContext() {
	const ctx = { currentTime: 0, sampleRate: 48000, state: 'suspended', destination: null };
	const param = () => ({ value: 0, setTargetAtTime(v) { this.value = v; }, setValueAtTime(v) { this.value = v; } });
	const node = (kind) => ({
		kind,
		context: ctx,
		gain: param(),
		delayTime: param(),
		fftSize: 32,
		connect() { return this; },
		disconnect() {}
	});
	ctx.destination = node('destination');
	ctx.createGain = () => node('gain');
	ctx.createDelay = () => node('delay');
	ctx.createChannelSplitter = () => node('splitter');
	ctx.createChannelMerger = () => node('merger');
	ctx.createAnalyser = () => node('analyser');
	return ctx;
}

let audio;

before(async () => {
	const store = new Map();
	globalThis.window = {
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	};
	globalThis.OPENDJ_CUE_SINK = { url: 'ws://127.0.0.1:4321/cue', token: 't' };
	globalThis.Worker = ReplyWorker;
	audio = await loadTypeScriptModule('src/lib/player/cue-align-audio.ts');
});

function readyForCalibration() {
	const hp = audio.mixerState.headphones;
	hp.outputs = [
		{ id: MAIN_ID, label: 'MacBook Pro Speakers' },
		{ id: CUE_ID, label: 'Headphones' }
	];
	hp.output_mode = 'two_outputs';
	hp.selected_output_device_id = CUE_ID;
	hp.routes.cue = { state: 'selected', selected: true };
}

test('a native MAIN select marks the route selected and calibration starts', async () => {
	audio.disposeHeadphoneMonitor();
	readyForCalibration();
	const ctx = fakeAudioContext();
	const source = () => ({ context: ctx, masterGain: ctx.createGain() });
	await audio.selectMasterOutput(MAIN_ID, source);
	const route = audio.mixerState.headphones.routes.master;
	assert.equal(route.state, 'selected');
	assert.equal(route.selected, true);
	assert.doesNotThrow(() => audio.cueAlignAudioEffects());

	for (const state of ['unsupported', 'failed']) {
		audio.mixerState.headphones.routes.master = { state, selected: true };
		assert.throws(() => audio.cueAlignAudioEffects(), new RegExp(`route capability is ${state}`));
	}
	audio.mixerState.headphones.routes.master = { state: 'default', selected: false };
	assert.throws(() => audio.cueAlignAudioEffects(), /still marked default/);
	assert.throws(() => audio.cueAlignAudioEffects(), (error) => {
		assert.equal(/Clear MAIN to use the OS default/.test(error.message), false);
		return true;
	});

	audio.disposeHeadphoneMonitor();
	audio.mixerState.headphones.selected_master_output_device_id = null;
	audio.mixerState.headphones.routes.master = { state: 'default', selected: false };
	globalThis.Worker = class extends ReplyWorker {
		constructor() {
			super();
			this.failNext = true;
		}
	};
	await assert.rejects(audio.selectMasterOutput(MAIN_ID, source), /master output selection failed/);
	assert.equal(audio.mixerState.headphones.routes.master.state, 'failed');
	assert.equal(audio.mixerState.headphones.selected_master_output_device_id, null);
});
