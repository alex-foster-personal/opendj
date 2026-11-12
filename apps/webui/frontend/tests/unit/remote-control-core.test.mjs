import assert from 'node:assert/strict';
import { before, describe, it, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import {
	ENABLED_QR_TARGETS,
	REMOTE_CONTROL_ROUTES,
	remoteControlStatusLine,
	remoteQrImageUrl
} from '../../src/lib/player/remote-control-core.ts';

/** Phone remote helpers (issue #10441): routes, status lines, poll stop. */

let core;

before(async () => {
	core = await loadTypeScriptModule('src/lib/player/remote-control-core.ts');
});

test('remoteControlRequest maps actions to the #10434 routes', () => {
	const start = core.remoteControlRequest('start');
	assert.equal(start.method, 'POST');
	assert.equal(core.REMOTE_CONTROL_ROUTES + start.path, '/api/v1/performance/remote-control/start');

	const status = core.remoteControlRequest('status');
	assert.equal(status.method, 'GET');
	assert.equal(core.REMOTE_CONTROL_ROUTES + status.path, '/api/v1/performance/remote-control');

	const stop = core.remoteControlRequest('stop');
	assert.equal(core.REMOTE_CONTROL_ROUTES + stop.path, '/api/v1/performance/remote-control/stop');

	const rotate = core.remoteControlRequest('rotate');
	assert.equal(
		core.REMOTE_CONTROL_ROUTES + rotate.path,
		'/api/v1/performance/remote-control/rotate-code'
	);
});

test('forget encodes the device id in the path', () => {
	const forget = core.remoteControlRequest('forget', 'dev 1/x');
	assert.equal(forget.method, 'DELETE');
	assert.equal(
		core.REMOTE_CONTROL_ROUTES + forget.path,
		'/api/v1/performance/remote-control/devices/dev%201%2Fx'
	);
	assert.throws(() => core.remoteControlRequest('forget'), /non-empty device_id/);
	assert.throws(() => core.remoteControlRequest('forget', ''), /non-empty device_id/);
});

test('remoteControlUrl builds the phone page URL', () => {
	const listening = {
		status: 'listening',
		code: 'K7Q-4MX',
		address: '192.168.1.20',
		address_error: null,
		port: 47810,
		devices: [],
		wrong_codes: 0,
		max_wrong_codes: 5
	};
	assert.equal(core.remoteControlUrl(listening), 'http://192.168.1.20:47810/remote');
	assert.equal(core.remoteControlUrl({ ...listening, address: null }), null);
});

test('remoteStatusLine covers off, listening, and locked_out', () => {
	const base = {
		code: null,
		address: null,
		address_error: null,
		port: null,
		devices: [],
		wrong_codes: 0,
		max_wrong_codes: 5
	};
	assert.equal(core.remoteStatusLine({ ...base, status: 'off' }), 'Phone remote is off.');
	assert.equal(
		core.remoteStatusLine({
			...base,
			status: 'listening',
			address: '10.0.0.2',
			port: 9000,
			wrong_codes: 1,
			max_wrong_codes: 5
		}),
		'Listening on 10.0.0.2:9000. Wrong codes: 1 of 5.'
	);
	assert.equal(
		core.remoteStatusLine({ ...base, status: 'listening', port: 9000, address_error: 'no iface' }),
		'Listening on port 9000, but no LAN address: no iface.'
	);
	assert.equal(
		core.remoteStatusLine({ ...base, status: 'locked_out', max_wrong_codes: 5 }),
		'Locked after 5 wrong codes. Start again to issue a new code.'
	);
	assert.throws(
		() => core.remoteStatusLine({ ...base, status: 'bogus' }),
		/unknown remote control status/
	);
});

test('relativeSeen and pairedDate format device timestamps', () => {
	const now = Date.parse('2026-11-10T20:00:00.000Z');
	assert.equal(core.relativeSeen('2026-11-10T19:59:50.000Z', now), 'just now');
	assert.equal(core.relativeSeen('2026-11-10T19:58:00.000Z', now), '2 min ago');
	assert.equal(core.relativeSeen('2026-11-10T17:00:00.000Z', now), '3 h ago');
	assert.equal(core.relativeSeen('2026-11-06T20:00:00.000Z', now), '4 d ago');
	assert.equal(core.relativeSeen('2026-11-10T20:01:00.000Z', now), 'just now');
	assert.equal(core.relativeSeen(null, now), 'never seen');
	assert.equal(core.relativeSeen('garbage', now), 'never seen');
	assert.equal(core.pairedDate('2026-11-10T19:52:09+00:00'), 'Tue 10 Nov 2026');
});

test('startStatusPoll stops after unmount and skips while refresh is pending', async () => {
	let refreshCalls = 0;
	const refresh = () => {
		refreshCalls += 1;
	};

	let intervalCb;
	let clearedWith;
	const fake = {
		setInterval(fn, ms) {
			intervalCb = fn;
			assert.equal(ms, 1000);
			return 'handle-1';
		},
		clearInterval(handle) {
			clearedWith = handle;
		}
	};

	const stop = core.startStatusPoll(refresh, fake, 1000);
	intervalCb();
	await Promise.resolve();
	intervalCb();
	await Promise.resolve();
	assert.equal(refreshCalls, 2);
	stop();
	assert.equal(clearedWith, 'handle-1');
	intervalCb();
	assert.equal(refreshCalls, 2);
	stop();

	let pendingCalls = 0;
	let release;
	const pendingRefresh = () =>
		new Promise((resolve) => {
			pendingCalls += 1;
			release = resolve;
		});
	let pendingCb;
	const fake2 = {
		setInterval(fn) {
			pendingCb = fn;
			return 'h2';
		},
		clearInterval() {}
	};
	const stop2 = core.startStatusPoll(pendingRefresh, fake2, 1000);
	pendingCb();
	pendingCb();
	assert.equal(pendingCalls, 1);
	release();
	stop2();
});

/** QR entry helpers (REMOTE-CTL / #10442). */
describe('remote-control-core', () => {
	it('cache-busts the QR image URL when the code rotates', () => {
		const a = remoteQrImageUrl('remote', 'K7Q4MX');
		const b = remoteQrImageUrl('remote', 'ABCD12');
		assert.notEqual(a, b);
		assert.equal(a, `${REMOTE_CONTROL_ROUTES}/qr.svg?target=remote&v=K7Q4MX`);
		assert.ok(ENABLED_QR_TARGETS.includes('remote'));
	});

	it('status line names listening address and reopened at boot', () => {
		const line = remoteControlStatusLine({
			status: 'listening',
			code: 'K7Q4MX',
			address: '192.0.2.5',
			address_error: null,
			port: 47810,
			devices: [],
			wrong_codes: 0,
			max_wrong_codes: 5,
			last_error: null,
			reopened_at_boot: true
		});
		assert.match(line, /192\.0\.2\.5:47810/);
		assert.match(line, /reopened at boot/);
	});
});
