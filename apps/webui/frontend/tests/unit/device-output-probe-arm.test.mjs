/**
 * Issue #923 (AUDIO-DEVICE-01): the OS output-device probe is loaded with a
 * dynamic import so it stays out of the first-paint "/" chunk. These tests arm
 * the REAL instrumentation module and prove the probe still installs for every
 * armed AudioContext, publishes into the output-health bar, and never installs
 * for a graph that was disarmed or re-armed while the import was in flight.
 *
 * [if] a graph is armed [then] once the import resolves the probe is registered
 *   for Switch output and publishes the merged verdict into the bar, [else stop].
 * [if] the graph is disarmed [then] the probe is unregistered and the bar is
 *   cleared, [else stop].
 * [if] a graph is disarmed before the import resolves [then] no probe is ever
 *   installed for it, [else stop].
 * [if] a second graph is armed after a disarm [then] it gets its own probe,
 *   [else stop].
 */
import assert from 'node:assert/strict';
import { after, before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/** The smallest AudioContext `armAudioContextWatchdog` touches while arming. */
function contextStandIn() {
	return {
		state: 'suspended',
		sampleRate: 48_000,
		baseLatency: 0.005,
		outputLatency: 0,
		currentTime: 0,
		addEventListener: () => {},
		removeEventListener: () => {},
		resume: async () => {},
		getOutputTimestamp: () => ({ contextTime: 0, performanceTime: 0 })
	};
}

/** Let the dynamic import (a resolved promise chain once bundled) settle. */
const settle = () => new Promise((resolve) => setImmediate(resolve));

describe('device output probe lazy install', () => {
	let mod;
	const hadWindow = 'window' in globalThis;

	before(async () => {
		// `armAudioContextWatchdog` publishes `window.__mdtAudioOutput`.
		if (!hadWindow) globalThis.window = globalThis;
		mod = await loadTypeScriptModule('tests/unit/fixtures/device-output-probe-arm-entry.ts');
	});

	after(() => {
		mod?.disarmContextInstrumentation();
		if (!hadWindow) delete globalThis.window;
	});

	it('installs the probe for an armed graph once its import resolves', async () => {
		mod.armAudioContextWatchdog(contextStandIn(), () => false);
		// The install moved behind an await: nothing is registered synchronously.
		assert.equal(mod.registeredDeviceOutputProbe(), null);
		await settle();
		const probe = mod.registeredDeviceOutputProbe();
		assert.ok(probe, 'if the lazily imported probe never installs then device faults go undetected - broken');
		assert.equal(typeof probe.switchOutput, 'function');

		// Installed AND wired: its publish lands in the bar's store.
		mod.audioOutputHealth.snapshot = null;
		probe.republish();
		assert.equal(
			mod.audioOutputHealth.snapshot?.combined_verdict,
			'idle',
			'if the installed probe does not publish into the output-health store then the bar never shows device state - broken'
		);
		mod.disarmContextInstrumentation();
	});

	it('unregisters the probe and clears the bar on disarm', async () => {
		mod.armAudioContextWatchdog(contextStandIn(), () => false);
		await settle();
		assert.ok(mod.registeredDeviceOutputProbe());
		mod.disarmContextInstrumentation();
		assert.equal(
			mod.registeredDeviceOutputProbe(),
			null,
			'if a disarmed graph keeps its probe registered then Switch output acts on a torn-down probe - broken'
		);
		assert.equal(mod.audioOutputHealth.snapshot, null);
	});

	it('never installs a probe for a graph disarmed while its import was in flight', async () => {
		mod.armAudioContextWatchdog(contextStandIn(), () => false);
		mod.disarmContextInstrumentation();
		await settle();
		assert.equal(
			mod.registeredDeviceOutputProbe(),
			null,
			'if a superseded import still installs then a closed graph polls the OS probe forever - broken'
		);
	});

	it('installs a fresh probe for the next graph after a disarm', async () => {
		mod.armAudioContextWatchdog(contextStandIn(), () => false);
		await settle();
		const first = mod.registeredDeviceOutputProbe();
		mod.disarmContextInstrumentation();
		mod.armAudioContextWatchdog(contextStandIn(), () => false);
		await settle();
		const second = mod.registeredDeviceOutputProbe();
		assert.ok(second, 'if a re-armed graph gets no probe then only the first route visit is covered - broken');
		assert.notEqual(second, first);
		mod.disarmContextInstrumentation();
	});
});
