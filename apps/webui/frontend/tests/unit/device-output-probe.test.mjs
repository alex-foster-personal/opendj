/**
 * [if] device stops delivering while playing [then] probe surfaces not_delivering within 10 s
 * [if] the browser verdict turns dead or escalates [then] the probe requests one OS probe
 *   per edge, including a dead verdict that predates install, [else stop].
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

describe('installDeviceOutputProbe SLA', () => {
	let installDeviceOutputProbe;
	let DEVICE_PROBE_POLL_MS;
	let DEVICE_PROBE_DEBOUNCE_MS;
	let DEVICE_PROBE_SLA_MS;
	let SILENCE_RMS_FLOOR;

	before(async () => {
		const mod = await loadTypeScriptModule('src/lib/rb/device-output-probe.ts');
		const silence = await loadTypeScriptModule('src/lib/rb/silence-watchdog.ts');
		installDeviceOutputProbe = mod.installDeviceOutputProbe;
		DEVICE_PROBE_POLL_MS = mod.DEVICE_PROBE_POLL_MS;
		DEVICE_PROBE_DEBOUNCE_MS = mod.DEVICE_PROBE_DEBOUNCE_MS;
		DEVICE_PROBE_SLA_MS = mod.DEVICE_PROBE_SLA_MS;
		SILENCE_RMS_FLOOR = silence.SILENCE_RMS_FLOOR;
	});

	it('surfaces not_delivering within the 10 s SLA after a stuck device verdict', async () => {
		let now = 0;
		const timeouts = [];
		const intervals = [];
		const updates = [];
		let fetchCount = 0;

		const notDelivering = {
			device_delivering: false,
			verdict: 'not_delivering',
			reason: 'stuck',
			default_device_name: 'Speakers',
			default_device_uid: 'uid',
			io_cycles_advanced: false,
			hal_overload_recent: false,
			probe_available: true,
			checked_at: '2026-09-20T00:00:00.000Z'
		};

		const handle = installDeviceOutputProbe(
			() => ({
				verdict: 'ok',
				output_latency_s: 0.02,
				output_latency_dead: false,
				output_position_ms: 1000,
				output_position_stalled: false,
				checked_at: '2026-09-20T00:00:00.000Z'
			}),
			() => true,
			() => false,
			() => SILENCE_RMS_FLOOR * 10,
			{
				fetchHealth: async () => {
					fetchCount += 1;
					return fetchCount >= 2 ? notDelivering : {
						...notDelivering,
						device_delivering: true,
						verdict: 'ok',
						reason: null
					};
				},
				switchOutput: async () => ({ cycled: true }),
				setInterval: (fn, ms) => {
					const id = intervals.push({ fn, ms, next: now + ms });
					return id;
				},
				clearInterval: (handle) => {
					const index = intervals.findIndex((entry) => entry === handle);
					if (index >= 0) intervals.splice(index, 1);
				},
				setTimeout: (fn, ms) => {
					const id = { fn, at: now + ms };
					timeouts.push(id);
					return id;
				},
				clearTimeout: (handle) => {
					const index = timeouts.findIndex((entry) => entry === handle);
					if (index >= 0) timeouts.splice(index, 1);
				},
				now: () => now,
				onUpdate: (snapshot) => {
					updates.push({ at: now, combined: snapshot.combined_verdict });
				}
			}
		);

		const flushPromises = () => new Promise((resolve) => setImmediate(resolve));

		const advance = async (ms) => {
			now += ms;
			for (const timeout of timeouts.splice(0)) {
				if (timeout.at <= now) timeout.fn();
			}
			for (const interval of intervals) {
				while (interval.next <= now) {
					interval.fn();
					interval.next += interval.ms;
				}
			}
			await flushPromises();
		};

		handle.requestProbe('test-fault');
		await advance(DEVICE_PROBE_DEBOUNCE_MS);
		await advance(DEVICE_PROBE_POLL_MS);

		const faultAt = updates.find((entry) => entry.combined === 'not_delivering')?.at;
		assert.equal(typeof faultAt, 'number', updates.map((entry) => `${entry.at}:${entry.combined}`).join(', '));
		assert.ok(faultAt <= DEVICE_PROBE_SLA_MS, `fault at ${faultAt}ms exceeds ${DEVICE_PROBE_SLA_MS}ms SLA`);

		handle.uninstall();
	});
});

describe('installDeviceOutputProbe browser dead-verdict edge', () => {
	let installDeviceOutputProbe;

	before(async () => {
		installDeviceOutputProbe = (await loadTypeScriptModule('src/lib/rb/device-output-probe.ts'))
			.installDeviceOutputProbe;
	});

	/** A probe whose debounced OS requests are counted, driven by `verdict`. */
	function harness(initialVerdict) {
		const state = { verdict: initialVerdict, requests: 0 };
		const handle = installDeviceOutputProbe(
			() => (state.verdict === null ? null : { verdict: state.verdict }),
			() => false,
			() => false,
			() => 0,
			{
				fetchHealth: async () => {
					throw new Error('not reached: timers are never fired here');
				},
				switchOutput: async () => ({ cycled: true }),
				setInterval: () => 1,
				clearInterval: () => {},
				// requestProbe is the only caller: one call is one OS probe request.
				setTimeout: () => {
					state.requests += 1;
					return state.requests;
				},
				clearTimeout: () => {},
				now: () => 0,
				onUpdate: () => {}
			}
		);
		return { state, handle };
	}

	it('requests an OS probe once when the browser verdict turns dead, not on every publish', () => {
		const { state, handle } = harness('ok');
		handle.republish();
		assert.equal(state.requests, 0, 'if an ok browser verdict requests an OS probe then every tick probes - broken');
		state.verdict = 'dead';
		handle.republish();
		assert.equal(state.requests, 1, 'if a dead browser verdict does not ask the OS then gate C cannot tell device from browser - broken');
		handle.republish();
		handle.republish();
		assert.equal(state.requests, 1, 'if every publish of a still-dead verdict re-requests then the probe hammers CoreAudio - broken');
		state.verdict = 'dead-escalated';
		handle.republish();
		assert.equal(state.requests, 2, 'if the escalation edge does not re-probe then a stuck device is never re-checked - broken');
		handle.uninstall();
	});

	it('acts on a dead verdict that was already standing when the probe installed', () => {
		const { state, handle } = harness('dead');
		assert.equal(state.requests, 0, 'install alone must not publish');
		handle.republish();
		assert.equal(
			state.requests,
			1,
			'if a dead verdict seen before the lazy install is ignored then that outage never reaches the OS probe - broken'
		);
		handle.uninstall();
	});
});

describe('isHalOverloadPerfEvent', () => {
	let isHalOverloadPerfEvent;

	before(async () => {
		const mod = await loadTypeScriptModule('src/lib/rb/device-output-probe.ts');
		isHalOverloadPerfEvent = mod.isHalOverloadPerfEvent;
	});

	it('matches dedicated hal-overload kind', () => {
		assert.equal(isHalOverloadPerfEvent('hal-overload', 'anything'), true);
	});

	it('matches HAL overload log signatures in message text', () => {
		assert.equal(
			isHalOverloadPerfEvent(
				'processor-latency-read-failed',
				'HALC_ProxyIOContext::IOWorkLoop: skipping cycle due to overload'
			),
			true
		);
	});
});
