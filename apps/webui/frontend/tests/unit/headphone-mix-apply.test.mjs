// requirement: UX-EXPLAIN-02
// [if] practice MAIN, MIX full cue, graph built [then] applyHeadphoneMix drives practiceCueMix gain, else stop
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

function installFakeWindow() {
	const store = new Map();
	globalThis.window = {
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	};
}

function fakeAudioContext() {
	const ctx = { currentTime: 0, sampleRate: 48000 };
	const param = () => {
		const p = {
			value: 0,
			setTargetAtTime(v) {
				p.value = v;
			},
			setValueAtTime(v) {
				p.value = v;
			}
		};
		return p;
	};
	const node = (kind, extra = {}) => ({
		kind,
		context: ctx,
		gain: param(),
		connect() {},
		disconnect() {},
		...extra
	});
	ctx.destination = node('destination');
	ctx.createGain = () => node('gain');
	ctx.createDelay = () => node('delay', { delayTime: param() });
	ctx.createChannelSplitter = () => node('splitter');
	ctx.createChannelMerger = () => node('merger');
	ctx.createMediaStreamDestination = () => node('msd', { stream: { getTracks: () => [] } });
	ctx.createAnalyser = () => node('analyser');
	return { ctx };
}

let headphones;
let mixerState;

before(async () => {
	installFakeWindow();
	globalThis.Audio = class {
		pause() {}
	};
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	const state = await loadTypeScriptModule('src/lib/player/state.svelte.ts');
	mixerState = state.mixerState;
});

test('applyHeadphoneMix sets practice cue blend when MAIN practice has full cue mix', () => {
	headphones.disposeHeadphoneMonitor();
	const { ctx } = fakeAudioContext();
	const masterGain = ctx.createGain();
	const nodes = headphones.ensureHeadphoneGraph(ctx, masterGain);

	mixerState.headphones.output_mode = 'practice';
	mixerState.headphones.selected_output_device_id = null;
	mixerState.headphones.mix = 0;
	mixerState.headphones.level = 1;
	mixerState.headphones.active = false;
	mixerState.headphones.head_delay_ms = 0;

	headphones.applyHeadphoneMix();

	assert.equal(nodes.practiceCueMix.gain.value, 1, 'practice cue must reach speakers when MIX is full cue');
	assert.equal(nodes.practiceMasterMix.gain.value, 1);
	assert.equal(nodes.cueMix.gain.value, 0, 'monitor cue path stays off without two_outputs');
});
