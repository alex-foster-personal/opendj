/**
 * [if] device stops delivering while playing [then] probe surfaces not_delivering within 10 s
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
