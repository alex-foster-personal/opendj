// requirement: CUEOUT-22
// [if] the shell announced no OPENDJ_CUE_SINK [then] nativeCueSinkConfig is null and the Chrome route is kept
// [if] OPENDJ_CUE_SINK names a non-loopback socket or has no token [then] nativeCueSinkConfig throws instead of falling back
// [if] a native output id is handed to the shell [then] the prefix is stripped, and a browser deviceId is refused
// [if] 128-frame worklet chunks reach the relay [then] they leave in fixed 256-frame batches, in order, nothing lost
// [if] the shell answers a request [then] the reply resolves the request with the same id, and an error reply rejects it
// [if] the shell socket drops [then] every pending request rejects and a disconnected event is emitted
// [if] MASTER is unpinned in the Mac app and a cue is selected [then] MASTER auto-pins to the current macOS default output, not a guessed speaker
// [if] the current macOS default output IS the selected cue device [then] MASTER falls back to the speaker guess, never the cue device
// [if] the shell names outputs but the webview withholds its inputs [then] the listing stays withheld and the panel asks for access
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let sink;
let sinkClient;
let headphones;
let ioAccess;

before(async () => {
	sink = await loadTypeScriptModule('src/lib/player/cue-native-sink.ts');
	sinkClient = await loadTypeScriptModule('src/lib/player/cue-native-sink-client.ts');
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	ioAccess = await loadTypeScriptModule('src/lib/player/io-device-access.ts');
});

test('no announcement means no native route', () => {
	assert.equal(sink.nativeCueSinkConfig({}), null);
	assert.equal(sink.nativeCueSinkConfig({ OPENDJ_CUE_SINK: null }), null);
});

test('a valid announcement parses, a malformed one throws', () => {
	assert.deepEqual(
		sink.nativeCueSinkConfig({ OPENDJ_CUE_SINK: { url: 'ws://127.0.0.1:51234/cue', token: 'abc' } }),
		{ url: 'ws://127.0.0.1:51234/cue', token: 'abc' }
	);
	assert.throws(
		() => sink.nativeCueSinkConfig({ OPENDJ_CUE_SINK: { url: 'ws://evil.example:1/cue', token: 'abc' } }),
		/loopback/
	);
	assert.throws(() => sink.nativeCueSinkConfig({ OPENDJ_CUE_SINK: { url: 'ws://127.0.0.1:1/cue', token: '' } }), /token/);
});

test('native ids round-trip and browser ids are refused', () => {
	const id = sink.nativeDeviceId('BuiltInSpeakerDevice');
	assert.equal(id, 'native:BuiltInSpeakerDevice');
	assert.equal(sink.nativeDeviceUid(id), 'BuiltInSpeakerDevice');
	assert.throws(() => sink.nativeDeviceUid('b8f2c0d1e3'), /not a native output id/);
	assert.throws(() => sink.nativeDeviceUid('native:'), /not a native output id/);
	assert.deepEqual(
		sink.nativeOutputsAsHeadphoneOutputs([
			{ uid: 'spk', name: 'MacBook Pro Speakers', channels: 2, transport: 'builtin', is_default: true }
		]),
		[{ id: 'native:spk', label: 'MacBook Pro Speakers' }]
	);
});

test('the batcher emits fixed batches in order across chunk boundaries', () => {
	const batcher = new sink.PcmBatcher(256);
	const sent = [];
	let next = 0;
	for (let chunk = 0; chunk < 5; chunk += 1) {
		const data = new Float32Array(128 * 2);
		for (let i = 0; i < data.length; i += 1) data[i] = next++;
		sent.push(...batcher.push(data));
	}
	assert.equal(sent.length, 2, '640 frames make two whole 256-frame batches');
	const flat = sent.flatMap((buffer) => Array.from(new Float32Array(buffer)));
	assert.equal(flat.length, 2 * 256 * 2);
	flat.forEach((value, index) => assert.equal(value, index, `sample ${index} out of order`));
	assert.throws(() => batcher.push(new Float32Array(3)), /whole stereo frames/);
});

class FakeWorker {
	constructor() {
		this.posted = [];
		this.onmessage = null;
		this.onerror = null;
		this.terminated = false;
	}
	postMessage(message) {
		this.posted.push(message);
	}
	terminate() {
		this.terminated = true;
	}
	deliver(message) {
		this.onmessage({ data: message });
	}
	lastCommand() {
		return this.posted.filter((m) => m.kind === 'command').at(-1).payload;
	}
}

const CONFIG = { url: 'ws://127.0.0.1:4321/cue', token: 't' };

async function flush() {
	for (let i = 0; i < 5; i += 1) await Promise.resolve();
}

test('requests resolve by id and error replies reject', async () => {
	const worker = new FakeWorker();
	const client = new sinkClient.NativeCueSinkClient(CONFIG, worker);
	assert.deepEqual(worker.posted[0], { kind: 'connect', url: CONFIG.url, token: 't' });
	worker.deliver({ kind: 'open' });

	const listing = client.list();
	await flush();
	const listId = worker.lastCommand().id;
	assert.equal(worker.lastCommand().type, 'list');
	worker.deliver({ kind: 'message', payload: { type: 'devices', id: listId, devices: [{ uid: 'a' }] } });
	assert.deepEqual(await listing, [{ uid: 'a' }]);

	const opening = client.open('native:phones', 48000);
	await flush();
	const open = worker.lastCommand();
	assert.equal(open.uid, 'phones', 'the shell gets the bare uid');
	assert.equal(open.sample_rate, 48000);
	worker.deliver({ kind: 'message', payload: { type: 'error', id: open.id, message: 'no output device with uid phones' } });
	await assert.rejects(opening, /no output device with uid phones/);
	assert.equal(client.opened, null, 'a failed open records nothing');
});

test('a dropped socket rejects pending requests and emits disconnected', async () => {
	const worker = new FakeWorker();
	const client = new sinkClient.NativeCueSinkClient(CONFIG, worker);
	const events = [];
	client.onEvent((event) => events.push(event));
	worker.deliver({ kind: 'open' });
	const pending = client.setMaster('native:spk');
	await flush();
	worker.deliver({ kind: 'closed', reason: 'socket closed (1006)' });
	await assert.rejects(pending, /disconnected/);
	assert.deepEqual(events, [{ type: 'disconnected', reason: 'socket closed (1006)' }]);
	assert.equal(client.disconnected, 'socket closed (1006)');
});

test('unsolicited shell events reach listeners', () => {
	const worker = new FakeWorker();
	const client = new sinkClient.NativeCueSinkClient(CONFIG, worker);
	const events = [];
	client.onEvent((event) => events.push(event.type));
	worker.deliver({ kind: 'message', payload: { type: 'device_lost', uid: 'phones' } });
	worker.deliver({ kind: 'message', payload: { type: 'devices_changed', devices: [] } });
	assert.deepEqual(events, ['device_lost', 'devices_changed']);
});

const NATIVE_OUTPUTS = [
	{ id: 'native:scarlett', label: 'Scarlett 2i2 USB' },
	{ id: 'native:spk', label: 'MacBook Pro Speakers' },
	{ id: 'native:airpods', label: 'AirPods Pro' }
];

test('an unpinned MASTER follows the current macOS output, not the speaker guess', () => {
	const assignment = headphones.dualSinkAssignment({
		outputs: NATIVE_OUTPUTS,
		selectedCueId: 'native:airpods',
		selectedMasterId: null,
		currentRoomId: 'native:scarlett'
	});
	assert.deepEqual(assignment, { masterId: 'native:scarlett', cueId: 'native:airpods', autoPinnedMaster: true });
});

test('when the macOS output is the cue device, MASTER falls back to speakers, never the cue', () => {
	const assignment = headphones.dualSinkAssignment({
		outputs: NATIVE_OUTPUTS,
		selectedCueId: 'native:airpods',
		selectedMasterId: null,
		currentRoomId: 'native:airpods'
	});
	assert.equal(assignment.masterId, 'native:spk');
	assert.notEqual(assignment.masterId, assignment.cueId);
});

test('control: a pinned MASTER and the Chrome call shape are unchanged', () => {
	assert.deepEqual(
		headphones.dualSinkAssignment({
			outputs: NATIVE_OUTPUTS,
			selectedCueId: 'native:airpods',
			selectedMasterId: 'native:spk',
			currentRoomId: 'native:scarlett'
		}),
		{ masterId: 'native:spk', cueId: 'native:airpods', autoPinnedMaster: false },
		'an operator pin is never overridden by the room'
	);
	assert.equal(
		headphones.dualSinkAssignment({ outputs: NATIVE_OUTPUTS, selectedCueId: 'native:airpods', selectedMasterId: null })
			.masterId,
		'native:spk',
		'without currentRoomId the speaker guess is unchanged'
	);
});

test('withheld webview inputs keep the Mac app listing in permission_needed', () => {
	const outputs = [{ id: 'native:spk', label: 'MacBook Pro Speakers' }];
	const hidden = ioAccess.listNativeShellDevices(outputs, [
		{ kind: 'audioinput', deviceId: '', label: '' },
		{ kind: 'audiooutput', deviceId: '', label: '' }
	]);
	assert.deepEqual(hidden.outputs, outputs);
	assert.deepEqual(hidden.inputs, []);
	assert.equal(hidden.names_withheld, true);
	const access = ioAccess.ioDeviceAccessForListing({ listing: hidden, permission: 'prompt', outputPinning: true });
	assert.equal(access.status, 'permission_needed');
	// Control: named inputs (or a webview output placeholder alone) read as listed.
	const named = ioAccess.listNativeShellDevices(outputs, [
		{ kind: 'audioinput', deviceId: 'mic1', label: 'MacBook Pro Microphone' },
		{ kind: 'audiooutput', deviceId: '', label: '' }
	]);
	assert.equal(named.names_withheld, false);
	assert.deepEqual(named.inputs, [{ id: 'mic1', label: 'MacBook Pro Microphone' }]);
	assert.equal(
		ioAccess.ioDeviceAccessForListing({ listing: named, permission: 'prompt', outputPinning: true }).status,
		'listed'
	);
});
