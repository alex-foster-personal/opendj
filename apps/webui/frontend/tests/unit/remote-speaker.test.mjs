import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * I18 remote speaker (REMOTE-SPK-8119e..8119h), the parts that need no browser.
 *
 * Regression lines:
 * - if the Opus fmtp loses stereo or the 256 kbit/s ceiling then MAIN goes out as a mono voice call
 * - if a description with no Opus is passed through then the link is not the codec the ADR names
 * - if the flag reads anything but `on` as on then a built app opens a LAN listener unasked
 * - if stop/unlink refuse while the flag is off then the engine's idempotent stop answers 400
 * - if the panel loses its inert entry then a built app shows a control with nothing behind it
 * - if the receiving browser refuses playback then the link reads linked over a silent speaker
 * - if the route tears down then a peer keeps playing off /performance and reads linked
 * The two-Chromium audio proof is tests/live/remote-speaker-loopback.mjs.
 */

let core;
let flag;
let orders;

before(async () => {
	core = await loadTypeScriptModule('src/lib/player/remote-speaker-core.ts');
	flag = await loadTypeScriptModule('src/lib/player/remote-speaker-flag.ts');
	orders = await loadTypeScriptModule('src/lib/player/remote-speaker-orders.ts');
});

// The audio section of a Chrome offer, as createOffer() writes it (CRLF).
const CHROME_AUDIO = [
	'v=0',
	'o=- 4611731400430051336 2 IN IP4 127.0.0.1',
	's=-',
	't=0 0',
	'm=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 0 8',
	'a=rtpmap:111 opus/48000/2',
	'a=rtcp-fb:111 transport-cc',
	'a=fmtp:111 minptime=10;useinbandfec=1',
	'a=rtpmap:63 red/48000/2',
	'a=fmtp:63 111/111',
	''
].join('\r\n');

test('Opus fmtp gains stereo both ways and the 256 kbit/s ceiling, keeping its own params', () => {
	const out = core.withOpusMusicParams(CHROME_AUDIO);
	const fmtp = out.split('\r\n').filter((line) => line.startsWith('a=fmtp:111 '));
	assert.deepEqual(fmtp, ['a=fmtp:111 minptime=10;useinbandfec=1;stereo=1;sprop-stereo=1;maxaveragebitrate=256000']);
	// Only Opus is touched: RED keeps its own format line.
	assert.ok(out.includes('a=fmtp:63 111/111\r\n'));
	assert.ok(out.endsWith('\r\n'));
	assert.equal(out.split('\r\n').length, CHROME_AUDIO.split('\r\n').length);
});

test('our Opus params win over conflicting ones, and the munge is idempotent', () => {
	const clash = CHROME_AUDIO.replace('minptime=10;useinbandfec=1', 'stereo=0;maxaveragebitrate=32000');
	const once = core.withOpusMusicParams(clash);
	assert.match(once, /a=fmtp:111 stereo=1;maxaveragebitrate=256000;sprop-stereo=1\r\n/);
	assert.equal(core.withOpusMusicParams(once), once);
});

test('an Opus section with no fmtp line gets one right after its rtpmap', () => {
	const bare = 'v=0\nm=audio 9 UDP/TLS/RTP/SAVPF 109\na=rtpmap:109 opus/48000/2\na=sendrecv\n';
	const out = core.withOpusMusicParams(bare).split('\n');
	assert.equal(out[3], 'a=fmtp:109 stereo=1;sprop-stereo=1;maxaveragebitrate=256000');
	assert.equal(out[4], 'a=sendrecv');
});

test('a description with no Opus is refused, not passed through', () => {
	assert.equal(core.opusPayloadType('v=0\r\na=rtpmap:0 PCMU/8000\r\n'), null);
	assert.throws(() => core.withOpusMusicParams('v=0\r\na=rtpmap:0 PCMU/8000\r\n'), /no Opus codec/);
});

test('the build flag is on only when it says on; unset reads off; junk throws', () => {
	assert.equal(flag.remoteSpeakerFlag('on'), 'on');
	assert.equal(flag.remoteSpeakerFlag('off'), 'off');
	assert.equal(flag.remoteSpeakerFlag(undefined), 'off');
	for (const bad of ['true', '1', 'ON', true, null]) {
		assert.throws(() => flag.remoteSpeakerFlag(bad), /VITE_REMOTE_SPEAKER must be on or off/);
	}
});

test('address and code checks match the engine rule', () => {
	for (const good of ['192.168.1.20:47800', '[::1]:5', 'dj-laptop.local:1', ' 10.0.0.5:65535 ']) {
		assert.equal(core.isSpeakerAddress(good), true, good);
	}
	for (const bad of ['', 'host', ':80', 'host:0', 'host:65536', '::1:80', 'host:abc', 'a b:80', 7]) {
		assert.equal(core.isSpeakerAddress(bad), false, String(bad));
	}
	assert.equal(core.isSpeakerCode('012345'), true);
	for (const bad of ['12345', '1234567', '12a456', 123456]) assert.equal(core.isSpeakerCode(bad), false);
});

test('peer states map to the engine report vocabulary; new reports nothing; unknown throws', () => {
	for (const state of ['connecting', 'connected', 'disconnected', 'failed', 'closed']) {
		assert.equal(core.peerReportState(state), state);
	}
	assert.equal(core.peerReportState('new'), null);
	assert.throws(() => core.peerReportState('checking'), /unknown RTCPeerConnection state/);
});

test('status lines name address, port, code and the delay', () => {
	const base = { code: '042042', address: '192.168.1.20', address_error: null, port: 47800, peer: null, wrong_codes: 0, max_wrong_codes: 5, last_error: null };
	assert.equal(core.speakerStatusLine({ ...base, status: 'listening' }), 'Listening on 192.168.1.20:47800, code 042042.');
	assert.equal(core.speakerStatusLine({ ...base, status: 'linked', peer: '192.168.1.9' }), 'Playing MAIN from 192.168.1.9.');
	assert.match(core.speakerStatusLine({ ...base, status: 'locked_out', wrong_codes: 5 }), /Locked out after 5 wrong codes/);
	assert.match(core.speakerStatusLine({ ...base, address: null, address_error: 'no route', status: 'listening' }), /no LAN address: no route/);
	assert.equal(core.linkStatusLine({ status: 'linked', address: '192.168.1.20:47800', error: null }), 'Sending MAIN to 192.168.1.20:47800 (delayed).');
	assert.match(core.linkStatusLine({ status: 'failed', address: 'x:1', error: 'wrong code' }), /failed: wrong code/);
	assert.match(core.REMOTE_SPEAKER_DELAY_TITLE, /delayed/);
});

function env(flag) {
	return {
		flag,
		engine: { apiBase: '', fetch: () => Promise.reject(new Error('no engine in this test')) },
		masterTap: () => {
			throw new Error('no master in this test');
		},
		masterSinkId: () => null
	};
}

test('with the flag off every start command is refused with the build reason', async () => {
	for (const command of [
		{ type: 'remote_speaker_receive' },
		{ type: 'remote_speaker_answer', sdp: 'v=0' },
		{ type: 'remote_speaker_link', address: '127.0.0.1:47800', code: '123456' }
	]) {
		await assert.rejects(orders.runRemoteSpeakerCommand(command, env('off')), /off in this build/);
	}
});

test('stop and unlink succeed with the flag off (nothing can be running)', async () => {
	assert.equal(await orders.runRemoteSpeakerCommand({ type: 'remote_speaker_receive_stop' }, env('off')), null);
	assert.equal(await orders.runRemoteSpeakerCommand({ type: 'remote_speaker_unlink' }, env('off')), null);
});

test('with the flag on, bad arguments and a missing WebRTC fail loud', async () => {
	await assert.rejects(orders.runRemoteSpeakerCommand({ type: 'remote_speaker_bogus' }, env('on')), /unknown remote speaker command/);
	await assert.rejects(
		orders.runRemoteSpeakerCommand({ type: 'remote_speaker_link', address: 'nope', code: '123456' }, env('on')),
		/needs address/
	);
	await assert.rejects(
		orders.runRemoteSpeakerCommand({ type: 'remote_speaker_link', address: '127.0.0.1:1', code: '12' }, env('on')),
		/6-digit code/
	);
	// Node has no RTCPeerConnection: being a speaker is refused, never faked.
	await assert.rejects(orders.runRemoteSpeakerCommand({ type: 'remote_speaker_receive' }, env('on')), /needs epoch/);
	await assert.rejects(orders.runRemoteSpeakerCommand({ type: 'remote_speaker_receive', epoch: 'e1' }, env('on')), /no WebRTC/);
	await assert.rejects(orders.runRemoteSpeakerCommand({ type: 'remote_speaker_answer', sdp: 'v=0' }, env('on')), /needs epoch and link_id/);
	await assert.rejects(
		orders.runRemoteSpeakerCommand({ type: 'remote_speaker_link', address: '127.0.0.1:1', code: '123456' }, env('on')),
		/needs link_id/
	);
	await assert.rejects(orders.runRemoteSpeakerCommand({ type: 'remote_speaker_answer_cancel' }, env('off')), /needs link_id/);
	assert.equal(await orders.runRemoteSpeakerCommand({ type: 'remote_speaker_answer_cancel', link_id: 'gone' }, env('off')), null);
	await assert.rejects(
		orders.runRemoteSpeakerCommand({ type: 'remote_speaker_answer', sdp: 'v=0', epoch: 'e1', link_id: 'l1' }, env('on')),
		/not being a speaker/
	);
});

test('every command type is one the engine route table names', () => {
	const routes = readFileSync(new URL('../../../server/routes/performance_remote_speaker.py', import.meta.url), 'utf8');
	for (const type of orders.REMOTE_SPEAKER_COMMAND_TYPES) assert.ok(routes.includes(`"${type}"`), type);
});

test('the panel keeps the inert Remote control entry when the flag is off, and built apps default off', () => {
	const cluster = readFileSync(new URL('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte', import.meta.url), 'utf8');
	// The section loads only with the flag on, and starts loading when the cluster mounts.
	assert.match(cluster, /const remoteSpeakerSection = remoteSpeakerOn \? import\('\.\/RemoteSpeakerSection\.svelte'\) : null;/);
	assert.match(cluster, /\{#if remoteSpeakerSection\}[\s\S]*\{#await remoteSpeakerSection then[\s\S]*\{:else\}[\s\S]*disabled title=\{plannedTitle\('remote-control'\)\}>Remote control</);
	const vite = readFileSync(new URL('../../vite.config.ts', import.meta.url), 'utf8');
	assert.match(vite, /devConfig === null\s*\?\s*'off'\s*:\s*'on'/);
	assert.match(vite, /'import\.meta\.env\.VITE_REMOTE_SPEAKER': JSON\.stringify\(remoteSpeaker\)/);
});

test('ICE gathering: send once host candidates settle, never with none', () => {
	const v = (probe) => core.iceGatheringVerdict({ state: 'gathering', candidates: 0, msSinceStart: 0, msSinceLastCandidate: null, ...probe });
	// Chrome with no STUN can sit at `gathering` forever after its host candidate.
	assert.equal(v({ candidates: 1, msSinceStart: 600, msSinceLastCandidate: core.ICE_SETTLE_MS }), 'done');
	assert.equal(v({ candidates: 1, msSinceStart: 100, msSinceLastCandidate: core.ICE_SETTLE_MS - 1 }), 'wait');
	assert.equal(v({ candidates: 0, msSinceStart: core.ICE_GATHER_TIMEOUT_MS - 1 }), 'wait');
	assert.equal(v({ candidates: 0, msSinceStart: core.ICE_GATHER_TIMEOUT_MS }), 'fail');
	assert.equal(v({ state: 'complete', candidates: 2, msSinceLastCandidate: 0 }), 'done');
	assert.equal(v({ state: 'complete', candidates: 0 }), 'fail');
});

test('with no bundler env at all the build flag reads off (the SSR panel harness case)', () => {
	assert.equal(flag.remoteSpeakerBuildFlag(), 'off');
});

test('an offer from an ended or different speaker session is refused before any connection is built', async () => {
	let built = 0;
	globalThis.RTCPeerConnection = class {
		constructor() {
			built += 1;
			throw new Error('a connection was built');
		}
	};
	try {
		const answer = (epoch) =>
			orders.runRemoteSpeakerCommand({ type: 'remote_speaker_answer', sdp: 'v=0', epoch, link_id: 'l1' }, env('on'));
		await orders.runRemoteSpeakerCommand({ type: 'remote_speaker_receive', epoch: 'session-a' }, env('on'));
		await assert.rejects(answer('session-b'), /speaker session that has ended/);
		// Control: the current epoch gets past the check (to the stub, which throws).
		await assert.rejects(answer('session-a'), /a connection was built/);
		assert.equal(built, 1);
		await orders.runRemoteSpeakerCommand({ type: 'remote_speaker_receive_stop' }, env('on'));
		await assert.rejects(answer('session-a'), /not being a speaker/);
		assert.equal(built, 1);
	} finally {
		delete globalThis.RTCPeerConnection;
	}
});

test('the speaker routes a MAIN picked as "default" to the empty sink id and passes other ids through (CUEOUT-27)', async () => {
	assert.equal(core.speakerSinkId('default'), '');
	assert.equal(core.speakerSinkId('usb-interface-1'), 'usb-interface-1');
	// Wiring: the answer path hands setSinkId the normalized id. The fake
	// <audio> stops the answer at setSinkId, so no connection is negotiated.
	const sinks = [];
	globalThis.RTCPeerConnection = class {
		close() {}
	};
	globalThis.Audio = class {
		pause() {}
		async setSinkId(id) {
			sinks.push(id);
			throw new Error('stop after setSinkId');
		}
	};
	try {
		for (const picked of ['default', 'usb-interface-1']) {
			const speakerEnv = { ...env('on'), masterSinkId: () => picked };
			await orders.runRemoteSpeakerCommand({ type: 'remote_speaker_receive', epoch: 'session-a' }, speakerEnv);
			await assert.rejects(
				orders.runRemoteSpeakerCommand(
					{ type: 'remote_speaker_answer', sdp: 'v=0', epoch: 'session-a', link_id: 'l1' },
					speakerEnv
				),
				/stop after setSinkId/
			);
			await orders.runRemoteSpeakerCommand({ type: 'remote_speaker_receive_stop' }, speakerEnv);
		}
		assert.deepEqual(sinks, ['', 'usb-interface-1']);
	} finally {
		delete globalThis.RTCPeerConnection;
		delete globalThis.Audio;
	}
});

/** A peer that answers in-process: the offer delivers one track, one host candidate is gathered. */
function answeringPeer(peers) {
	return class {
		constructor() {
			this.listeners = {};
			this.connectionState = 'new';
			this.iceGatheringState = 'new';
			this.signalingState = 'stable';
			this.localDescription = null;
			this.closed = false;
			peers.push(this);
		}
		addEventListener(type, fn) {
			(this.listeners[type] ??= []).push(fn);
		}
		removeEventListener(type, fn) {
			this.listeners[type] = (this.listeners[type] ?? []).filter((f) => f !== fn);
		}
		async setRemoteDescription() {
			for (const fn of this.listeners.track ?? []) fn({ streams: [{ id: 'remote' }], track: {} });
		}
		async createAnswer() {
			return { type: 'answer', sdp: 'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=rtpmap:111 opus/48000/2\r\n' };
		}
		async setLocalDescription(description) {
			this.localDescription = { sdp: `${description.sdp}a=candidate:1 1 udp 1 127.0.0.1 9 typ host\r\n` };
			this.iceGatheringState = 'complete';
			for (const fn of this.listeners.icecandidate ?? []) fn({ candidate: { candidate: 'candidate:1' } });
		}
		close() {
			this.closed = true;
			this.signalingState = 'closed';
		}
	};
}

/** Run `fn(rs, reports, audios, peers)` with a fake browser; `play` is what audio.play() returns. */
async function onFakeSpeaker(play, fn) {
	const rs = await loadTypeScriptModule('src/lib/player/remote-speaker.ts');
	const reports = [];
	const audios = [];
	const peers = [];
	globalThis.RTCPeerConnection = answeringPeer(peers);
	globalThis.Audio = class {
		constructor() {
			this.paused = false;
			this.srcObject = null;
			audios.push(this);
		}
		play() {
			return play();
		}
		pause() {
			this.paused = true;
		}
	};
	globalThis.AudioContext = class {
		constructor() {
			this.state = 'running';
		}
		createAnalyser() {
			return { fftSize: 0 };
		}
		createMediaStreamSource() {
			return { connect() {} };
		}
		close() {
			this.state = 'closed';
			return Promise.resolve();
		}
	};
	const engine = {
		apiBase: '',
		fetch: async (url, init) => {
			reports.push({ url, body: JSON.parse(init.body) });
			return { ok: true, status: 200 };
		}
	};
	try {
		await fn(rs, reports, audios, peers, engine);
	} finally {
		rs.disableSpeaker();
		delete globalThis.RTCPeerConnection;
		delete globalThis.Audio;
		delete globalThis.AudioContext;
	}
}

const answerOptions = (engine) => ({ sdp: 'v=0', epoch: 'session-a', linkId: 'l1', sinkId: null, engine });

test('a speaker whose browser refuses playback fails the link and tells the engine why', async () => {
	await onFakeSpeaker(
		() => Promise.reject(new Error('NotAllowedError: play() needs a gesture')),
		async (rs, reports, audios, peers, engine) => {
			rs.enableSpeaker('session-a');
			await assert.rejects(rs.answerSpeakerOffer(answerOptions(engine)), /cannot start playback.*NotAllowedError/);
			const failed = reports.filter((r) => r.url.endsWith('/speaker/report'));
			assert.equal(failed.length, 1, 'one failure report');
			assert.equal(failed[0].body.state, 'failed');
			assert.equal(failed[0].body.link_id, 'l1');
			assert.match(failed[0].body.error, /cannot start playback/);
			assert.equal(peers[0].closed, true, 'the silent peer is dropped');
			assert.equal(audios[0].paused, true);
			assert.equal(rs.remoteSpeakerReceiverLevel().reason, 'no link');
		}
	);
});

test('control: a speaker that plays answers and reports no failure', async () => {
	await onFakeSpeaker(
		() => Promise.resolve(),
		async (rs, reports, audios, peers, engine) => {
			rs.enableSpeaker('session-a');
			const sdp = await rs.answerSpeakerOffer(answerOptions(engine));
			assert.match(sdp, /a=candidate:/);
			assert.deepEqual(reports, []);
			assert.equal(peers[0].closed, false);
		}
	);
});

test('route teardown closes the speaker link, reports it closed and stops being a speaker', async () => {
	await onFakeSpeaker(
		() => Promise.resolve(),
		async (rs, reports, audios, peers, engine) => {
			rs.enableSpeaker('session-a');
			await rs.answerSpeakerOffer(answerOptions(engine));
			rs.closeRemoteSpeakerLinks();
			assert.equal(peers[0].closed, true, 'nothing keeps playing off the route');
			assert.equal(audios[0].paused, true);
			assert.deepEqual(
				reports.map((r) => [r.url.slice(r.url.lastIndexOf('/', r.url.lastIndexOf('/') - 1)), r.body.state, r.body.link_id]),
				[['/speaker/report', 'closed', 'l1']]
			);
			await assert.rejects(rs.answerSpeakerOffer(answerOptions(engine)), /not being a speaker/);
			rs.closeRemoteSpeakerLinks();
			assert.equal(reports.length, 1, 'a second teardown reports nothing');
		}
	);
});

test('route teardown stops the engine session this page enabled, even with no link', async () => {
	await onFakeSpeaker(
		() => Promise.resolve(),
		async (rs, reports, audios, peers, engine) => {
			rs.enableSpeaker('session-a', engine);
			rs.closeRemoteSpeakerLinks();
			await new Promise((resolve) => setTimeout(resolve, 0));
			assert.deepEqual(
				reports.map((r) => [r.url, r.body]),
				[['/api/v1/performance/remote-speaker/speaker/stop', { epoch: 'session-a' }]],
				'the listener must not outlive the page'
			);
			rs.closeRemoteSpeakerLinks();
			assert.equal(reports.length, 1, 'a second teardown stops nothing');
		}
	);
});

test('control: stopping on the speaker command path sends no teardown stop', async () => {
	await onFakeSpeaker(
		() => Promise.resolve(),
		async (rs, reports, audios, peers, engine) => {
			rs.enableSpeaker('session-a', engine);
			rs.disableSpeaker();
			rs.closeRemoteSpeakerLinks();
			await new Promise((resolve) => setTimeout(resolve, 0));
			assert.deepEqual(reports, [], 'the engine already stopped this session');
		}
	);
});

test('a local description that fails stops ICE polling and leaves no unhandled rejection', async () => {
	const unhandled = [];
	const onUnhandled = (reason) => unhandled.push(reason);
	process.on('unhandledRejection', onUnhandled);
	try {
		await onFakeSpeaker(
			() => Promise.resolve(),
			async (rs, reports, audios, peers, engine) => {
				const Base = globalThis.RTCPeerConnection;
				globalThis.RTCPeerConnection = class extends Base {
					async setLocalDescription() {
						throw new Error('setLocalDescription refused');
					}
				};
				rs.enableSpeaker('session-a');
				await assert.rejects(rs.answerSpeakerOffer(answerOptions(engine)), /setLocalDescription refused/);
				await new Promise((resolve) => setTimeout(resolve, 200));
				assert.equal(peers[0].closed, true);
				assert.deepEqual(peers[0].listeners.icecandidate ?? [], [], 'gathering stopped once the peer closed');
			}
		);
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.deepEqual(unhandled, []);
	} finally {
		process.off('unhandledRejection', onUnhandled);
	}
});

test('the /performance route teardown runs the route-owned closers, and the speaker registers its own', () => {
	const read = (path) => readFileSync(new URL(`../../${path}`, import.meta.url), 'utf8');
	const page = read('src/routes/performance/+page.svelte');
	const cleanup = page.slice(page.indexOf('unmounted = true;'));
	// The links close inside the unmount cleanup, ahead of the engine teardown it schedules.
	assert.ok(cleanup.slice(0, cleanup.indexOf('beginTeardown()')).includes('closeRouteOwnedLinks();'));
	assert.match(read('src/lib/player/remote-speaker.ts'), /^closeWithPerformanceRoute\(closeRemoteSpeakerLinks\);$/m);
});

test('route-owned closers all run on each teardown, and one that throws stops none', async () => {
	const links = await loadTypeScriptModule('src/lib/player/route-owned-links.ts');
	const ran = [];
	links.closeWithPerformanceRoute(() => {
		ran.push('a');
		throw new Error('a would not close');
	});
	links.closeWithPerformanceRoute(() => ran.push('b'));
	const errors = console.error;
	console.error = () => {};
	try {
		links.closeRouteOwnedLinks();
		links.closeRouteOwnedLinks();
	} finally {
		console.error = errors;
	}
	assert.deepEqual(ran, ['a', 'b', 'a', 'b']);
});
