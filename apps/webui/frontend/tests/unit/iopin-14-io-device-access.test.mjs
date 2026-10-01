// requirement: IOPIN-14 (the audio I/O panel never draws an unreadable device list as an empty one)
// [if] the browser returns its empty-id placeholders [then] no blank row is listed and the system default output is, with a name ⛔️
// [if] the device names are withheld [then] the state is permission_needed (grant) or permission_denied (retry), never listed ⛔️
// [if] the device API is missing, rejects, or hangs [then] each is its own named state with a retry, and the system default is still listed ⛔️
// [if] a listing fails [then] the plug-in event that could fix it is still being watched ⛔️
// [if] a refresh is refused before it runs [then] the refusal is recorded as a state, not swallowed ⛔️
// [if] a saved output is gone [then] a notice names it and the system default fallback; a renamed-id device is matched by label ⛔️
// [if] I/O opens in a `request` build and the browser has never been asked [then] access is requested once, the stream is stopped, and the devices are named ⛔️
// [if] I/O opens again after the grant [then] no stream is opened, so nothing can prompt ⛔️
// [if] I/O opens in a `button` build [then] no stream is ever opened by opening; only the grant button asks ⛔️
//
// Regression line: if `refreshHeadphoneOutputs` maps enumerateDevices() straight into the menu then
// Chromium's pre-permission placeholders ({deviceId: '', label: ''}) become blank rows, and a failed
// or never-run enumeration leaves three empty menus with no word of why (observed live Thu 1 Oct 2026).
//
// The enumerator is injected here (a fake `navigator.mediaDevices`), which is what a unit test may
// do. The real device API is exercised by tests/e2e/performance-io-devices.spec.ts.
import assert from 'node:assert/strict';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let access;
let headphones;
let playerState;
let refresh;

// What Chromium really returns before a media permission: one entry per kind,
// EMPTY id and label. (An earlier fixture used `deviceId: 'default'` here,
// which is why the blank rows were never seen by a test.)
const CHROMIUM_WITHHELD = [
	{ kind: 'audioinput', deviceId: '', label: '' },
	{ kind: 'videoinput', deviceId: '', label: '' },
	{ kind: 'audiooutput', deviceId: '', label: '' }
];
const CHROMIUM_GRANTED = [
	{ kind: 'audioinput', deviceId: 'default', label: 'Default - Built-in Microphone' },
	{ kind: 'audioinput', deviceId: 'mic-1', label: 'Built-in Microphone' },
	{ kind: 'audiooutput', deviceId: 'default', label: 'Default - Built-in Speakers' },
	{ kind: 'audiooutput', deviceId: 'spk-1', label: 'Built-in Speakers' },
	{ kind: 'audiooutput', deviceId: 'usb-1', label: 'USB Interface' }
];
// WebKit lists inputs only: it has no `audiooutput` kind to enumerate.
const WEBKIT_GRANTED = [{ kind: 'audioinput', deviceId: 'mic-1', label: 'Built-in Microphone' }];

const savedNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');

function installNavigator({ enumerateDevices, permission = null }) {
	const listeners = [];
	const mediaDevices =
		enumerateDevices === undefined
			? undefined
			: {
					enumerateDevices,
					addEventListener: (type, handler) => listeners.push({ type, handler }),
					removeEventListener() {}
				};
	const navigator = { mediaDevices };
	if (permission !== null) navigator.permissions = { query: async () => ({ state: permission }) };
	Object.defineProperty(globalThis, 'navigator', { value: navigator, configurable: true, writable: true });
	return listeners;
}

function hp() {
	return playerState.mixerState.headphones;
}

before(async () => {
	access = await loadTypeScriptModule('src/lib/player/io-device-access.ts');
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	playerState = await loadTypeScriptModule('src/lib/player/state.svelte.ts');
	refresh = await loadTypeScriptModule('src/lib/rb/io-device-refresh.ts');
});

afterEach(() => {
	headphones.disposeHeadphoneMonitor();
	if (savedNavigator === undefined) delete globalThis.navigator;
	else Object.defineProperty(globalThis, 'navigator', savedNavigator);
	const state = hp();
	state.outputs = [];
	state.inputs = [];
	state.error = null;
	state.supported = false;
	state.device_access = access.ioDeviceAccessNotChecked();
	state.selected_output_device_id = null;
	state.selected_master_output_device_id = null;
	state.selected_input_device_id = null;
});

// ------------------------------------------------------------- pure listing

test('if the browser returns empty-id placeholders then no blank row is listed and the system default is named', () => {
	const listing = access.listIoDevices(CHROMIUM_WITHHELD);
	assert.deepEqual(listing.outputs, [{ id: 'default', label: 'System default output' }]);
	assert.deepEqual(listing.inputs, []);
	assert.equal(listing.names_withheld, true);
	for (const device of [...listing.outputs, ...listing.inputs]) {
		assert.notEqual(device.id.trim(), '', 'a listed device must have an id');
		assert.notEqual(device.label.trim(), '', 'a listed device must have a label');
	}
});

test('if the browser names its devices then they are listed as given, default included once', () => {
	const listing = access.listIoDevices(CHROMIUM_GRANTED);
	assert.deepEqual(
		listing.outputs.map((output) => output.label),
		['Default - Built-in Speakers', 'Built-in Speakers', 'USB Interface']
	);
	assert.equal(listing.outputs.filter((output) => output.id === 'default').length, 1);
	assert.equal(listing.names_withheld, false);
});

test('if the engine enumerates no outputs at all then the system default output is still listed', () => {
	const listing = access.listIoDevices(WEBKIT_GRANTED);
	assert.deepEqual(listing.outputs, [{ id: 'default', label: 'System default output' }]);
	assert.equal(listing.inputs.length, 1);
	assert.equal(listing.names_withheld, false);
});

test('if a device has an id but no name then it is listed with a stand-in name and marked withheld', () => {
	const listing = access.listIoDevices([
		{ kind: 'audiooutput', deviceId: 'default', label: '' },
		{ kind: 'audiooutput', deviceId: 'abc', label: '' }
	]);
	assert.deepEqual(listing.outputs, [
		{ id: 'default', label: 'System default output' },
		{ id: 'abc', label: 'Output device 2 (name hidden)' }
	]);
	assert.equal(listing.names_withheld, true);
});

// ---------------------------------------------------------- access states

test('if names are withheld then the state is a grant or a retry, never listed', () => {
	const listing = access.listIoDevices(CHROMIUM_WITHHELD);
	const prompt = access.ioDeviceAccessForListing({ listing, permission: 'prompt', outputPinning: true });
	assert.equal(prompt.status, 'permission_needed');
	assert.equal(prompt.action, 'grant');
	const unknown = access.ioDeviceAccessForListing({ listing, permission: null, outputPinning: true });
	assert.equal(unknown.status, 'permission_needed');
	const denied = access.ioDeviceAccessForListing({ listing, permission: 'denied', outputPinning: true });
	assert.equal(denied.status, 'permission_denied');
	assert.equal(denied.action, 'retry');
});

test('if an engine returns an EMPTY array before a grant then that is withheld, not a machine with no devices', () => {
	const listing = access.listIoDevices([]);
	assert.equal(access.ioDeviceAccessForListing({ listing, permission: null, outputPinning: true }).status, 'permission_needed');
	// With the grant on record an empty machine is a real answer.
	assert.equal(access.ioDeviceAccessForListing({ listing, permission: 'granted', outputPinning: true }).status, 'listed');
});

test('if the listing is real then the state is listed with no action and no message', () => {
	const listing = access.listIoDevices(CHROMIUM_GRANTED);
	const listed = access.ioDeviceAccessForListing({ listing, permission: 'granted', outputPinning: true });
	assert.deepEqual(
		{ status: listed.status, action: listed.action, message: listed.message, notices: listed.notices },
		{ status: 'listed', action: 'none', message: null, notices: [] }
	);
});

test('every state that is not listed has its own message and an action', () => {
	const statuses = ['not_checked', 'permission_needed', 'permission_denied', 'api_missing', 'enumeration_failed', 'timeout'];
	const messages = statuses.map((status) => access.ioDeviceAccessMessage(status));
	assert.equal(new Set(messages).size, statuses.length, 'two states share one message');
	for (const status of statuses) {
		assert.notEqual(access.ioDeviceAccessAction(status), 'none', `${status} offers no action`);
		assert.match(access.ioDeviceAccessMessage(status), /system default output/, `${status} must say what is playing`);
	}
	assert.throws(() => access.ioDeviceAccessMessage('fine'), /unknown I\/O device access status/);
});

test('if the shell cannot pin an output then a standing notice says audio follows the system default', () => {
	const listing = access.listIoDevices(WEBKIT_GRANTED);
	const state = access.ioDeviceAccessForListing({ listing, permission: 'granted', outputPinning: false });
	assert.equal(state.status, 'listed');
	assert.equal(state.output_pinning, false);
	assert.deepEqual(state.notices, [access.OUTPUT_PINNING_UNSUPPORTED_NOTICE]);
});

test('if the permission reads granted but names are still withheld then the grant button asks instead of doing nothing', () => {
	assert.equal(headphones.labelUnlockDecision('granted', false), 'already_unlocked');
	assert.equal(headphones.labelUnlockDecision('granted', true), 'ask');
	assert.equal(headphones.labelUnlockDecision('denied', true), 'declined', 'a refusal is never asked again');
	assert.equal(headphones.labelUnlockDecision('prompt', true), 'ask');
});

// ------------------------------------------------- the enumeration itself

test('if Chromium withholds names then refresh lists the system default and names the permission state', async () => {
	installNavigator({ enumerateDevices: async () => CHROMIUM_WITHHELD, permission: 'prompt' });
	await headphones.refreshHeadphoneOutputs();
	assert.deepEqual(hp().outputs, [{ id: 'default', label: 'System default output' }]);
	assert.equal(hp().device_access.status, 'permission_needed');
	assert.equal(hp().device_access.action, 'grant');
});

test('if access is denied then refresh says so and offers a retry', async () => {
	installNavigator({ enumerateDevices: async () => CHROMIUM_WITHHELD, permission: 'denied' });
	await headphones.refreshHeadphoneOutputs();
	assert.equal(hp().device_access.status, 'permission_denied');
	assert.equal(hp().device_access.action, 'retry');
	assert.equal(hp().outputs.length, 1);
});

test('if access is granted then refresh lists every named device and the state is listed', async () => {
	installNavigator({ enumerateDevices: async () => CHROMIUM_GRANTED, permission: 'granted' });
	await headphones.refreshHeadphoneOutputs();
	assert.equal(hp().device_access.status, 'listed');
	assert.deepEqual(
		hp().outputs.map((output) => output.id),
		['default', 'spk-1', 'usb-1']
	);
});

test('if the device API is missing then the state is api_missing and the system default is still listed', async () => {
	installNavigator({ enumerateDevices: undefined });
	await assert.rejects(() => headphones.refreshHeadphoneOutputs(), /navigator\.mediaDevices is unavailable/);
	assert.equal(hp().device_access.status, 'api_missing');
	assert.equal(hp().device_access.action, 'retry');
	assert.match(hp().device_access.detail, /mediaDevices is unavailable/);
	assert.deepEqual(hp().outputs, [{ id: 'default', label: 'System default output' }]);
});

test('if enumeration rejects then the state is enumeration_failed with the cause, and hotplug is still watched', async () => {
	const listeners = installNavigator({
		enumerateDevices: async () => {
			throw Object.assign(new Error('device hub offline'), { name: 'NotReadableError' });
		}
	});
	await assert.rejects(() => headphones.refreshHeadphoneOutputs(), /enumeration failed: device hub offline/);
	assert.equal(hp().device_access.status, 'enumeration_failed');
	assert.equal(hp().device_access.detail, 'device hub offline');
	assert.deepEqual(hp().outputs, [{ id: 'default', label: 'System default output' }]);
	assert.deepEqual(
		listeners.map((listener) => listener.type),
		['devicechange'],
		'a failed listing must still be retried by the plug-in event that fixes it'
	);
});

test('if enumeration hangs then the state is timeout, distinct from a rejection', async (t) => {
	t.mock.timers.enable({ apis: ['setTimeout'] });
	installNavigator({ enumerateDevices: () => new Promise(() => {}) });
	const pending = assert.rejects(() => headphones.refreshHeadphoneOutputs(), /timed out/);
	t.mock.timers.tick(60_000);
	await pending;
	assert.equal(hp().device_access.status, 'timeout');
	assert.equal(hp().device_access.action, 'retry');
	assert.deepEqual(hp().outputs, [{ id: 'default', label: 'System default output' }]);
});

test('if a later listing fails then the devices read earlier stay listed', async () => {
	let fail = false;
	installNavigator({
		enumerateDevices: async () => {
			if (fail) throw new Error('gone');
			return CHROMIUM_GRANTED;
		},
		permission: 'granted'
	});
	await headphones.refreshHeadphoneOutputs();
	fail = true;
	await assert.rejects(() => headphones.refreshHeadphoneOutputs());
	assert.equal(hp().device_access.status, 'enumeration_failed');
	assert.equal(hp().outputs.length, 3, 'a failed re-list must not erase what was read');
});

test('if the device list changes then the devicechange handler lists again', async () => {
	let devices = CHROMIUM_GRANTED;
	const listeners = installNavigator({ enumerateDevices: async () => devices, permission: 'granted' });
	await headphones.refreshHeadphoneOutputs();
	devices = CHROMIUM_GRANTED.filter((device) => device.deviceId !== 'usb-1');
	listeners.find((listener) => listener.type === 'devicechange').handler();
	await new Promise((resolve) => setImmediate(resolve));
	await new Promise((resolve) => setImmediate(resolve));
	assert.deepEqual(
		hp().outputs.map((output) => output.id),
		['default', 'spk-1']
	);
});

// ------------------------------------------------ a refresh that never ran

function refreshDeps(overrides) {
	const published = [];
	let attempts = 0;
	return {
		published,
		deps: {
			rustUnsupported: () => false,
			refresh: async () => {},
			request: async () => {
				throw new Error('request must not run here');
			},
			attempts: () => attempts,
			publishFailure: (status, error) => {
				attempts += 1;
				published.push({ status, message: error.message });
			},
			...overrides
		},
		bump: () => {
			attempts += 1;
		}
	};
}

test('if the refresh is refused before it runs then the refusal is recorded, not swallowed', async () => {
	const harness = refreshDeps({
		refresh: async () => {
			throw new Error('performance command session 0 was invalidated');
		}
	});
	await refresh.refreshIoDeviceList(harness.deps);
	assert.deepEqual(harness.published, [
		{ status: 'enumeration_failed', message: 'performance command session 0 was invalidated' }
	]);
});

test('if the refresh ran and failed then its own state stands and is not overwritten', async () => {
	const harness = refreshDeps({});
	harness.deps.refresh = async () => {
		harness.bump(); // the enumeration published its own outcome
		throw new Error('headphone output enumeration failed: timed out');
	};
	await refresh.refreshIoDeviceList(harness.deps);
	assert.deepEqual(harness.published, []);
});

test('if the Rust engine owns the output then the list says so instead of staying empty', async () => {
	const harness = refreshDeps({
		rustUnsupported: () => true,
		refresh: async () => {
			throw new Error('must not dispatch in Rust engine mode');
		}
	});
	await refresh.refreshIoDeviceList(harness.deps);
	assert.equal(harness.published.length, 1);
	assert.equal(harness.published[0].status, 'api_missing');
	assert.match(harness.published[0].message, /Rust audio engine/);
});

test('root cause: with no command session the dispatcher refuses a listing and the UI helper drops the refusal', async () => {
	const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	// The mixer mounts before the route starts its command session, so this is
	// the state the old mount-time listing ran in.
	await assert.rejects(
		() => ipc.dispatchPerformanceCommand({ type: 'headphone_outputs_refresh' }),
		/performance command session \d+ was invalidated/
	);
	await ipc.runPerformanceCommandFromUi({ type: 'headphone_outputs_refresh' });
	assert.equal(hp().device_access.status, 'not_checked', 'the dropped refusal leaves no trace, which is the defect');
	assert.deepEqual(hp().outputs, []);
});

// ----------------------------------------------------------- saved picks

test('if a saved output is gone then a notice names it and the system default fallback', () => {
	const outputs = access.listIoDevices(CHROMIUM_GRANTED).outputs;
	const notices = access.savedIoDeviceNotices({
		saved: { master: { id: 'gone-1', label: 'Club Mixer' }, cue: null },
		outputs,
		selectedMasterId: null,
		selectedCueId: null
	});
	assert.deepEqual(notices, ['Saved MASTER output "Club Mixer" is not connected. Using the system default output.']);
});

test('if a saved output is in use then there is no notice', () => {
	const outputs = access.listIoDevices(CHROMIUM_GRANTED).outputs;
	const notices = access.savedIoDeviceNotices({
		saved: { master: { id: 'usb-1', label: 'USB Interface' }, cue: null },
		outputs,
		selectedMasterId: 'usb-1',
		selectedCueId: null
	});
	assert.deepEqual(notices, []);
});

test('if the origin changed and re-salted the id then a saved output is still found by its name', () => {
	const outputs = access.listIoDevices(CHROMIUM_GRANTED).outputs;
	const saved = { id: 'id-from-another-port', label: 'USB Interface' };
	assert.equal(access.savedIoDeviceIsListed(saved, outputs), 'usb-1');
	const notices = access.savedIoDeviceNotices({
		saved: { master: saved, cue: null },
		outputs,
		selectedMasterId: null,
		selectedCueId: null
	});
	assert.deepEqual(notices, ['Saved MASTER output "USB Interface" is connected but not in use. Pick it below to use it.']);
});

// ------------------------------------- what opening I/O may ask for, by build

/** A browser whose device names follow its microphone permission, with every
 * stream it hands out recorded. `answer` is what the operator does with the
 * prompt: 'grant', 'deny', or an Error to reject with. */
function installPermissionedBrowser({ permission, answer, alreadyNamed = false }) {
	const browser = { permission, streamsOpened: 0, tracksStopped: 0, tracksLive: 0 };
	const mediaDevices = {
		enumerateDevices: async () =>
			alreadyNamed || browser.permission === 'granted' ? CHROMIUM_GRANTED : CHROMIUM_WITHHELD,
		getUserMedia: async () => {
			browser.streamsOpened += 1;
			if (answer === 'deny') {
				browser.permission = 'denied';
				throw Object.assign(new Error('Permission denied'), { name: 'NotAllowedError' });
			}
			if (answer instanceof Error) throw answer;
			browser.permission = 'granted';
			browser.tracksLive += 1;
			return {
				getTracks: () => [
					{
						stop() {
							browser.tracksStopped += 1;
							browser.tracksLive -= 1;
						}
					}
				]
			};
		},
		addEventListener() {},
		removeEventListener() {}
	};
	const navigator = { mediaDevices };
	if (permission !== null) navigator.permissions = { query: async () => ({ state: browser.permission }) };
	Object.defineProperty(globalThis, 'navigator', { value: navigator, configurable: true, writable: true });
	return browser;
}

/** The real listing functions behind the open helper, as the browser deps wire them. */
function openDeps() {
	return {
		rustUnsupported: () => false,
		refresh: () => headphones.refreshHeadphoneOutputs(),
		request: () => headphones.requestIoDeviceNames(),
		attempts: headphones.ioDeviceAccessAttempts,
		publishFailure: headphones.publishIoDeviceAccessFailure
	};
}

test('the open mode is one of two words; unset is button, anything else set is a config error', () => {
	assert.equal(access.ioDeviceAccessOnOpen('request'), 'request');
	assert.equal(access.ioDeviceAccessOnOpen('button'), 'button');
	assert.equal(access.ioDeviceAccessOnOpen(undefined), 'button');
	for (const bad of ['', 'auto', 'Request', null, true, 1]) {
		assert.throws(() => access.ioDeviceAccessOnOpen(bad), /VITE_IO_DEVICE_ACCESS_ON_OPEN must be one of request, button/);
	}
});

test('request build: if the browser has never been asked then opening I/O asks once, stops the stream, and names the devices', async () => {
	const browser = installPermissionedBrowser({ permission: 'prompt', answer: 'grant' });
	await refresh.listIoDevicesOnOpen('request', openDeps());
	assert.equal(browser.streamsOpened, 1);
	assert.equal(browser.tracksStopped, 1);
	assert.equal(browser.tracksLive, 0, 'the stream must not outlive the grant');
	assert.equal(hp().device_access.status, 'listed');
	assert.deepEqual(
		hp().outputs.map((output) => output.label),
		['Default - Built-in Speakers', 'Built-in Speakers', 'USB Interface']
	);
	// Opening I/O changes no route (IOPIN-03): the request adds nothing to what a
	// plain listing of the same devices settles on, and no cue device goes live.
	const afterOpen = [hp().selected_master_output_device_id, hp().selected_output_device_id, hp().active];
	await headphones.refreshHeadphoneOutputs();
	assert.deepEqual([hp().selected_master_output_device_id, hp().selected_output_device_id, hp().active], afterOpen);
	assert.equal(hp().active, false);
});

test('request build: if I/O opens again after the grant then no stream is opened, so nothing can prompt', async () => {
	const browser = installPermissionedBrowser({ permission: 'prompt', answer: 'grant' });
	await refresh.listIoDevicesOnOpen('request', openDeps());
	await refresh.listIoDevicesOnOpen('request', openDeps());
	await refresh.listIoDevicesOnOpen('request', openDeps());
	assert.equal(browser.streamsOpened, 1, 'a granted origin must be listed without a second request');
	assert.equal(hp().device_access.status, 'listed');
});

test('request build: if the operator refuses then the state is permission_denied with how to re-allow, and later opens do not ask', async () => {
	const browser = installPermissionedBrowser({ permission: 'prompt', answer: 'deny' });
	await refresh.listIoDevicesOnOpen('request', openDeps());
	assert.equal(hp().device_access.status, 'permission_denied');
	assert.match(hp().device_access.message, /Allow microphone access/);
	assert.equal(hp().error, null, 'a refusal is a state, not an error');
	assert.deepEqual(hp().outputs, [{ id: 'default', label: 'System default output' }]);
	await refresh.listIoDevicesOnOpen('request', openDeps());
	assert.equal(browser.streamsOpened, 1, 'a denied origin must not be asked again on open');
});

test('request build: if the permission state cannot be read as prompt then opening never asks', async () => {
	for (const permission of [null, 'denied']) {
		const browser = installPermissionedBrowser({ permission, answer: 'grant' });
		await refresh.listIoDevicesOnOpen('request', openDeps());
		assert.equal(browser.streamsOpened, 0, `permission ${String(permission)} must not open a stream on open`);
		assert.notEqual(hp().device_access.status, 'listed');
	}
});

test('request build: if the request itself faults then the panel says why and the list stays readable', async () => {
	const fault = Object.assign(new Error('Could not start audio source'), { name: 'NotReadableError' });
	installPermissionedBrowser({ permission: 'prompt', answer: fault });
	await refresh.listIoDevicesOnOpen('request', openDeps());
	assert.equal(hp().device_access.status, 'permission_needed');
	assert.match(hp().error, /audio device access request failed: Could not start audio source/);
	assert.deepEqual(hp().outputs, [{ id: 'default', label: 'System default output' }]);
});

test('button build: if the browser has never been asked then opening I/O opens no stream and offers the grant button', async () => {
	const browser = installPermissionedBrowser({ permission: 'prompt', answer: 'grant' });
	await refresh.listIoDevicesOnOpen('button', openDeps());
	await refresh.listIoDevicesOnOpen('button', openDeps());
	assert.equal(browser.streamsOpened, 0, 'a built app must never request access by opening I/O');
	assert.equal(hp().device_access.status, 'permission_needed');
	assert.equal(hp().device_access.action, 'grant');
});

test('either build: mounting never opens a stream', async () => {
	const browser = installPermissionedBrowser({ permission: 'prompt', answer: 'grant' });
	await refresh.refreshIoDeviceList(openDeps());
	assert.equal(browser.streamsOpened, 0);
	assert.equal(hp().device_access.status, 'permission_needed');
});

test('if the open mode is not one of the two then the helper throws instead of guessing', async () => {
	installPermissionedBrowser({ permission: 'prompt', answer: 'grant' });
	await assert.rejects(() => refresh.listIoDevicesOnOpen('auto', openDeps()), /unknown I\/O open mode: auto/);
});

test('request build: if the devices are already named then opening never asks, whatever the permission reads', async () => {
	// A one-time grant names the devices for the session while the Permissions
	// API goes back to `prompt`. Nothing is withheld, so there is nothing to ask for.
	const browser = installPermissionedBrowser({ permission: 'prompt', answer: 'grant', alreadyNamed: true });
	await refresh.listIoDevicesOnOpen('request', openDeps());
	assert.equal(browser.streamsOpened, 0);
	assert.equal(hp().device_access.status, 'listed');
});
