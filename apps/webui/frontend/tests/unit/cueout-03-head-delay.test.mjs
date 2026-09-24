// requirement: CUEOUT-03
// [if] head delay is set to N ms [then] the monitor output lags the cue bus by N ms within one buffer, measured by a loopback test against a click train
// [if] head delay is set outside 0-500 or non-finite [then] the setter throws and the previous value stands
// [if] the selected monitor device's label matches a Bluetooth transport [then] the warning names Bluetooth explicitly
// [if] the app restarts [then] the head delay value is restored from persisted mixer config
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const MIXER_CONFIG_STORAGE_KEY = 'mdt.rb.mixer-config.v1';
const SAMPLE_RATE = 48000;
const BUFFER_SIZE = 128;
const CLICK_OPTS = {
	sampleRate: SAMPLE_RATE,
	bufferSize: BUFFER_SIZE,
	clickPeriodMs: 100,
	clickCount: 4
};
const BUFFER_TOLERANCE_MS = (BUFFER_SIZE / SAMPLE_RATE) * 1000;

/** Minimal localStorage + window so persistence paths are live. */
function installFakeWindow() {
	const store = new Map();
	globalThis.window = {
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	};
	return store;
}

let headphones;
let ipc;
let mixerConfig;

before(async () => {
	installFakeWindow();
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	mixerConfig = await loadTypeScriptModule('src/lib/player/mixer-config.ts');
});

function assertLagNear(delayMs) {
	const lagMs = headphones.clickTrainLagMs({ delayMs, ...CLICK_OPTS });
	assert.ok(Math.abs(lagMs - delayMs) <= BUFFER_TOLERANCE_MS);
}

test('click train lag matches head delay within one buffer', () => {
	for (const delayMs of [0, 40, 500]) {
		assertLagNear(delayMs);
	}
});

test('monitor graph wires DelayNode after level with hard delayTime set', async () => {
	const [hpSrc, engineSrc] = await Promise.all([
		readFile('src/lib/player/headphones.ts', 'utf8'),
		readFile('src/lib/rb/audio-engine.svelte.ts', 'utf8')
	]);
	assert.match(hpSrc, /level\.connect\(delay\)/);
	assert.match(hpSrc, /delay\.connect\(bridgeInput\)/);
	assert.match(hpSrc, /createDelay\(HEAD_DELAY_MAX_MS \/ 1000\)/);
	assert.match(hpSrc, /delayTime\.setValueAtTime/);
	assert.doesNotMatch(hpSrc, /nodes\.delay\.delayTime\.setTargetAtTime/);
	assert.doesNotMatch(engineSrc, /createDelay/);
});

test('setHeadDelayMs rejects invalid values and leaves the previous value', () => {
	headphones.setHeadDelayMs(40);
	for (const bad of [501, -1, Number.NaN, Infinity, '40', null]) {
		assert.throws(() => headphones.setHeadDelayMs(bad), /head delay must be a finite number within 0\.\.500/i);
		assert.equal(ipc.queryPerformanceState().mixer.headphones.head_delay_ms, 40);
	}
	headphones.setHeadDelayMs(0);
	assert.equal(ipc.queryPerformanceState().mixer.headphones.head_delay_ms, 0);
	headphones.setHeadDelayMs(500);
	assert.equal(ipc.queryPerformanceState().mixer.headphones.head_delay_ms, 500);
});

test('head_delay_ms IPC rejects before engine write and round-trips legal values', async () => {
	globalThis.window = { localStorage: globalThis.window.localStorage };
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		headphones.setHeadDelayMs(40);
		const before = ipc.queryPerformanceState().mixer.headphones;
		await assert.rejects(
			ipc.dispatchPerformanceCommand({ type: 'head_delay_ms', value: 501 }),
			/head delay must be a finite number within 0\.\.500/i
		);
		assert.deepEqual(ipc.queryPerformanceState().mixer.headphones, before);
		await ipc.dispatchPerformanceCommand({ type: 'head_delay_ms', value: 123 });
		assert.equal(ipc.queryPerformanceState().mixer.headphones.head_delay_ms, 123);
	} finally {
		uninstall();
	}
});

test('Bluetooth warning helpers and HeadphoneCluster wiring', async () => {
	assert.equal(headphones.monitorLabelIsBluetooth('WH-1000XM5 (Bluetooth)'), true);
	assert.equal(headphones.monitorLabelIsBluetooth('AirPods Pro'), true);
	assert.equal(headphones.monitorLabelIsBluetooth('USB Audio Device'), false);
	assert.equal(headphones.monitorLabelIsBluetooth('BT-200'), false);
	assert.equal(headphones.monitorLabelIsBluetooth(''), false);

	assert.equal(
		headphones.twoOutputsWarning({ outputMode: 'practice', selectedLabel: 'AirPods Pro' }),
		null
	);
	const generic = headphones.twoOutputsWarning({
		outputMode: 'two_outputs',
		selectedLabel: 'USB Audio Device'
	});
	assert.match(generic, /6 ms\/min/);
	assert.match(generic, /100 ppm/);
	assert.match(generic, /auditioning, not beatmatching/i);

	const bt = headphones.twoOutputsWarning({
		outputMode: 'two_outputs',
		selectedLabel: 'AirPods Pro'
	});
	assert.match(bt, /Bluetooth/i);

	const clusterSrc = await readFile('src/lib/components/rb/mixer/HeadphoneCluster.svelte', 'utf8');
	assert.match(clusterSrc, /data-two-outputs-warning/);
	assert.match(clusterSrc, /aria-label="head delay milliseconds"/);
	assert.match(clusterSrc, /twoOutputsWarning\(/);
	assert.match(clusterSrc, /ondelay/);
});

describe('mixer config persistence', () => {
	test('missing key defaults to zero', () => {
		const store = installFakeWindow();
		assert.equal(mixerConfig.loadMixerConfig().head_delay_ms, 0);
		assert.equal(store.has(MIXER_CONFIG_STORAGE_KEY), false);
	});

	test('setHeadDelayMs writes and reloads persisted value', async () => {
		const store = installFakeWindow();
		const freshHeadphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
		freshHeadphones.setHeadDelayMs(123);
		assert.equal(store.get(MIXER_CONFIG_STORAGE_KEY), JSON.stringify({ head_delay_ms: 123 }));
		const reloaded = await loadTypeScriptModule('src/lib/player/mixer-config.ts');
		assert.equal(reloaded.loadMixerConfig().head_delay_ms, 123);
	});

	test('malformed blobs throw with recovery guidance', () => {
		const store = installFakeWindow();
		store.set(MIXER_CONFIG_STORAGE_KEY, JSON.stringify({ head_delay_ms: -5 }));
		assert.throws(() => mixerConfig.loadMixerConfig(), /malformed.*clear the localStorage key/i);
		store.set(MIXER_CONFIG_STORAGE_KEY, JSON.stringify('nope'));
		assert.throws(() => mixerConfig.loadMixerConfig(), /malformed.*clear the localStorage key/i);
	});

	test('absent localStorage yields defaults without throwing', async () => {
		delete globalThis.window;
		const noWindow = await loadTypeScriptModule('src/lib/player/mixer-config.ts');
		assert.equal(noWindow.loadMixerConfig().head_delay_ms, 0);
	});

	test('_defaultHeadphones restores persisted head delay', async () => {
		const store = installFakeWindow();
		store.set(MIXER_CONFIG_STORAGE_KEY, JSON.stringify({ head_delay_ms: 77 }));
		const state = await loadTypeScriptModule('src/lib/player/state.svelte.ts');
		assert.equal(state._defaultHeadphones().head_delay_ms, 77);
		assert.equal(state._defaultHeadphones().mix, 0);
	});
});
