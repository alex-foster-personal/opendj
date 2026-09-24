/**
 * [if] composition rows from issue #923 [then] foldDeviceDelivery matches table
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

function probe(delivering) {
	return {
		device_delivering: delivering,
		verdict: delivering ? 'ok' : 'not_delivering',
		reason: delivering ? null : 'stuck',
		default_device_name: 'Speakers',
		default_device_uid: 'uid',
		io_cycles_advanced: delivering,
		hal_overload_recent: false,
		probe_available: true,
		checked_at: '2026-09-20T00:00:00.000Z'
	};
}

describe('foldDeviceDelivery', () => {
	let fold;
	let silenceFloor;
	before(async () => {
		const mod = await loadTypeScriptModule('src/lib/rb/device-output-delivering.ts');
		const silence = await loadTypeScriptModule('src/lib/rb/silence-watchdog.ts');
		fold = mod.foldDeviceDelivery;
		silenceFloor = silence.SILENCE_RMS_FLOOR;
	});

	const active = () => ({
		playing: true,
		masterMuted: false,
		masterRms: silenceFloor * 10
	});

	it('gate D: muted => idle', () => {
		assert.equal(fold({ ...active(), masterMuted: true, browserLiveness: 'ok', probe: probe(true) }), 'idle');
	});

	it('gate D: not playing => idle', () => {
		assert.equal(fold({ ...active(), playing: false, browserLiveness: 'ok', probe: probe(false) }), 'idle');
	});

	it('gate C: dead + delivering => idle', () => {
		assert.equal(fold({ ...active(), browserLiveness: 'dead', probe: probe(true) }), 'idle');
	});

	it('gate C: dead-escalated + delivering => idle', () => {
		assert.equal(fold({ ...active(), browserLiveness: 'dead-escalated', probe: probe(true) }), 'idle');
	});

	it('dead + not delivering => not_delivering', () => {
		assert.equal(fold({ ...active(), browserLiveness: 'dead', probe: probe(false) }), 'not_delivering');
	});

	it('ok + not delivering => not_delivering', () => {
		assert.equal(fold({ ...active(), browserLiveness: 'ok', probe: probe(false) }), 'not_delivering');
	});

	it('stalled + not delivering => not_delivering', () => {
		assert.equal(fold({ ...active(), browserLiveness: 'stalled', probe: probe(false) }), 'not_delivering');
	});

	it('ok + delivering => ok', () => {
		assert.equal(fold({ ...active(), browserLiveness: 'ok', probe: probe(true) }), 'ok');
	});

	it('stalled + delivering => ok', () => {
		assert.equal(fold({ ...active(), browserLiveness: 'stalled', probe: probe(true) }), 'ok');
	});

	it('null probe => unknown', () => {
		assert.equal(fold({ ...active(), browserLiveness: 'ok', probe: null }), 'unknown');
	});

	it('probe error => unknown', () => {
		assert.equal(
			fold({
				...active(),
				browserLiveness: 'ok',
				probe: {
					device_delivering: null,
					verdict: 'unknown',
					reason: 'no shell',
					default_device_name: null,
					default_device_uid: null,
					io_cycles_advanced: null,
					hal_overload_recent: null,
					probe_available: false,
					checked_at: '2026-09-20T00:00:00.000Z'
				}
			}),
			'unknown'
		);
	});
});
