// requirement: CUEOUT-26 (MAIN and CUE on one output run as split cue)
// [if] a newer CUE pick lands while a same-device split CUE pick is still moving MAIN [then] the split publishes nothing: the newer CUE, its mode and its saved CUE stand
// [if] a newer CUE pick lands while a committed split is still closing the old CUE output [then] the split saves nothing over the newer CUE
// [if] a CUE pick starts while a MAIN pick's split is still silencing the old CUE output [then] that late silence is undone and the new CUE sounds
// [if] a newer CUE pick lands while an older ordinary CUE pick is opening its device [then] the older one publishes and saves nothing, and a failure it hits is recorded without rolling the newer one back
// [if] no newer CUE pick lands [then] a split CUE pick still publishes and saves its CUE (control)
// [if] a newer CUE pick lands while a MAIN pick onto the CUE's output is moving MAIN into a split [then] the split publishes nothing over the newer CUE
// [if] a CUE device is picked again while its own pick is still pending [then] the re-pick is a no-op that supersedes nothing (control against an overshoot)
// [if] any caller enters a same-device split [then] its freshness check includes the CUE serial
// [if] an older browser CUE pick fails after a newer one landed, so its rollback lands last [then] the cue sink is put back on the device the menu shows
// [if] an older browser CUE pick resumes the cue context after a newer split silenced it [then] the cue context is silenced again
// [if] the newest CUE pick fails and its rollback cannot restore (no superseded pick) [then] the cue stays silenced, never re-applied (control)
// [if] a stale CUE pick settles while a newer one is still in flight and that newer one then fails [then] nothing re-applies the newer pick's device over its rollback
// [if] a same-device split supersedes a pending MAIN pick and its own sink move fails [then] MAIN settles on the device the sink last landed on: reopened, published and saved
// [if] a split with no MAIN pick pending fails its sink move [then] MAIN is left as it was, with no reopen (control)
// [if] a newer MAIN pick starts while that settle reopens the room [then] the settle publishes and saves nothing
// [if] that failed split finds no landed move since a remount [then] MAIN goes back to the saved MAIN, pinned, reopened and published, never cleared
// [if] a CUE pick starts while the stale-pick correction's own sink move is pending, and that move fails [then] its rollback is corrected in turn and the sink ends on the newest CUE
// [if] that correction fails with no newer CUE pick [then] it is not retried in a loop, and the operator is told to pick the CUE again
// [if] the routes are torn down while a CUE pick is pending and it then fails [then] it rejects as stale and rolls back nothing over the replacement mount
// [if] a MAIN pick commits a split onto a device a CUE pick is still opening [then] that CUE pick is retired and republishes nothing over the split
// [if] a CUE pick lands while the correction's own split-silencing suspend is pending [then] the suspend landing last is corrected and the new CUE sounds
// [if] stale CUE picks settle and the CUE the menu shows is not active [then] the correction re-applies nothing and the cue stays silent
// [if] a MAIN pick's split fails to close the old CUE output [then] it still shows that the old output may sound
// [if] a CUE pick retires a MAIN pick's split while the split reopens the room it already moved [then] the sink is moved back to the MAIN the menu shows
// [if] a MAIN pick's split move fails after a teardown or a newer CUE pick retired it [then] it only logs, and shows no error over the newer state
// [if] a MAIN sink move lands after a teardown and a split on the next mount then fails [then] MAIN settles on the saved MAIN, pinned on the new room, never on the old mount's device
// [if] a newer CUE pick fails while an older one is still opening [then] the menu rolls back to the CUE last published, never the older pick's optimistic device
// [if] a split commits while CUE picks overlap and a later pick fails [then] it rolls back to the split, not the CUE before it (control)
// [if] a CUE pick lands while an older one is still opening and a later pick fails [then] it rolls back to the CUE that landed
// [if] the routes are torn down while a CUE pick is opening and a pick on the new mount fails [then] it rolls back to the CUE that mount showed
// [if] a retired mount's CUE pick is still pending while the replacement mount's own picks go stale [then] the replacement mount's cue sink is corrected without waiting for it, and it counts as none of that mount's pending work
// [if] a cue suspend that silences as CUE work never settles [then] its work is released after a bounded grace, so the CUE no longer reads as switching
// [if] such a suspend lands only after its grace, once a newer CUE pick has published [then] that CUE is still put back on and sounds
// [if] a newest pick's rollback silence lands only after its grace, with no newer pick [then] nothing re-applies the CUE over that silence (control against an overshoot)
// [if] such a late suspend lands while a newer CUE pick is still pending [then] its correction waits for that pick, never re-applying the pending device early
// [if] a retired mount's CUE pick settles while the replacement mount's own pick is pending [then] that pick still reads as pending (control against the old mount releasing the new mount's count)
// [if] a failed split's fallback move to the saved MAIN lands after a newer MAIN pick published [then] the sink is moved back to the MAIN the menu shows
// [if] a split's suspend of the old CUE output times out, a newer CUE pick lands, and the suspend then lands [then] the newer CUE is put back on and sounds
// [if] the correction's split-silencing suspend times out, a newer CUE pick lands, and the suspend then lands [then] the newer CUE is put back on and sounds
// [if] a failed pick's rollback-silencing suspend times out, a newer CUE pick lands, and the suspend then lands [then] the newer CUE is put back on and sounds
// [if] a retired mount's superseded CUE pick settles last and the replacement mount has no stale pick of its own [then] nothing re-applies the replacement mount's silenced CUE (control against an overshoot)
// [if] a vanished CUE's suspend times out, a newer CUE pick lands, and the suspend then lands [then] the newer CUE is put back on and sounds
// [if] two device changes re-pin MAIN and the older re-pin lands last [then] the sink is moved back to the MAIN shown
// [if] a device change silences a vanished CUE and a later one restores it before that silence lands [then] the restored CUE sounds
// [if] an older device listing returns after a newer one [then] it is dropped, and the routes follow the newer listing
// [if] an older device listing fails after a newer one succeeded [then] its failure is not shown over the newer listing
// [if] a device change's MAIN re-pin is pending before any deck built the live graph [then] a MAIN or AUDIO IN Test refuses rather than beeping a short-lived context
// [if] an older shell listing naming another room returns after a newer one [then] a later CUE pick still pins MAIN to the newer listing's room
// [if] a device change's MAIN re-pin times out, a newer MAIN pick lands, and the timed-out move then lands [then] the sink is moved back to the MAIN shown, and MAIN counts as switching until then
// [if] a timed-out MAIN move never settles [then] it stops counting as MAIN switching after the grace
// [if] a device change's MAIN re-pin starts while a Test is sounding [then] the Test is cut and answers not played; a device change that moves nothing leaves it playing
// [if] the Mac app has no live graph and the MAIN landing was forgotten by a remount [then] a MAIN or AUDIO IN Test refuses rather than beeping the OS default; one whose MAIN landed with no graph still plays
// [if] a retired mount's timed-out MAIN move is still pending after a teardown [then] the replacement mount reads no MAIN switch, and the old move's landing does not unbalance the count
// [if] the Mac shell's connection drops [then] the known MAIN landing is forgotten, so a MAIN Test refuses until MAIN is pinned again
// [if] a device change restores a CUE and a newer one removes it again before the restore lands [then] the late restore publishes nothing: the CUE stays cleared and off
// [if] a retired mount's ordinary MAIN move is still inside its timeout at a teardown [then] the replacement mount reads no MAIN switch, and the old move's end does not unbalance the count
// [if] a device change enters a same-device split and a newer device change lists the outputs apart before the split's MAIN move lands [then] the old split publishes nothing and MAIN is moved back to the MAIN shown
// [if] a lone device change lists MAIN and CUE on one physical output [then] its split still publishes split_cable (control against an overshoot)
// [if] a native CUE open times out and lands after a newer CUE pick, or after its own pick rolled back [then] the shell CUE is reopened on the CUE shown
// [if] a native CUE open lands inside its timeout with no newer pick [then] nothing more is sent (control)
// [if] a device change's MAIN re-pin is pending when a newer device change clears MAIN to the system default [then] the re-pin publishes no MAIN route, the browser sink goes back to the default, and its failure shows no error
// [if] a lone device change re-pins MAIN [then] that re-pin still publishes the MAIN route, and its failure is still reported (control)
// [if] a MAIN pick lands while that move back to the system default is pending [then] the sink is moved back to the MAIN picked once it lands
// [if] a MAIN pick's split cannot close the old CUE output and its room reopen also fails [then] both failures are shown
// [if] a device change re-pins MAIN [then] the room is reopened before the route is published, and a failed reopen shows the retry error
// [if] a MAIN pick lands while that re-pin reopens the room [then] the re-pin's late reopen failure is not shown (control)
// [if] the output a same-device split ran on vanishes with no output left for MAIN [then] the room is moved to the system default and reopened
// [if] a refresh lands after a failed room reopen [then] the retry error stays shown until a reopen succeeds
// [if] a timed-out MAIN move lands late on the MAIN still shown [then] nothing more is moved, and the room is reopened, counted as MAIN switching
// [if] the Mac shell loses the CUE device, drops the MAIN pin or its connection, or the cue bridge fails, while a Test is sounding [then] the Test is cut and answers not played
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, beforeEach, mock, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SPEAKERS = 'native:BuiltInSpeakerDevice';
const USB = 'native:UsbInterface';

/** Command types whose next reply is held in `held` until released. */
let holdTypes = [];
/** Held replies, in send order: `{ type, uid, ok(), fail(message) }`. */
const held = [];
/** Every native set_master uid, in send order. */
const setMasterUids = [];
/** The uid the native CUE is open on, by the shell's answers; null when closed. */
let nativeCueOpen = null;
/** Native CUE open and close commands sent, in send order. */
const cueCommands = [];
/** What the shell lists for `list`, read when the command is sent; null sends no devices. */
let nativeDevices = null;

class ReplyWorker {
	constructor() {
		ReplyWorker.last = this;
		this.onmessage = null;
		this.onerror = null;
	}
	postMessage(message) {
		queueMicrotask(() => {
			if (this.onmessage === null) return;
			if (message.kind === 'connect') {
				this.onmessage({ data: { kind: 'open' } });
				return;
			}
			if (message.kind !== 'command') return;
			const { id, type, uid } = message.payload;
			if (type === 'set_master') setMasterUids.push(uid);
			if (type === 'open' || type === 'close') cueCommands.push(type);
			const devices = type === 'list' && nativeDevices !== null ? { devices: nativeDevices } : {};
			const ok = () => {
				// Where the shell's CUE physically is: the last open or close it answered.
				if (type === 'open') nativeCueOpen = uid;
				if (type === 'close') nativeCueOpen = null;
				this.onmessage?.({ data: { kind: 'message', payload: { type: 'ok', id, ...devices } } });
			};
			const fail = (text) => this.onmessage?.({ data: { kind: 'message', payload: { type: 'error', id, message: text } } });
			const index = holdTypes.indexOf(type);
			if (index !== -1) {
				holdTypes.splice(index, 1);
				held.push({ type, uid, ok, fail });
				return;
			}
			ok();
		});
	}
	terminate() {}
}

class StubWorkletNode {
	constructor(context) {
		this.context = context;
		this.port = { postMessage() {}, close() {} };
	}
	connect() {
		return this;
	}
	disconnect() {}
}

function roomContext() {
	const ctx = { currentTime: 0, sampleRate: 48000, state: 'running', destination: null };
	ctx.addEventListener = () => {};
	ctx.removeEventListener = () => {};
	ctx.suspends = 0;
	ctx.suspend = async () => {
		ctx.suspends += 1;
		ctx.state = 'suspended';
	};
	ctx.resume = async () => {
		ctx.state = 'running';
	};
	ctx.audioWorklet = { addModule: async () => {} };
	ctx.getOutputTimestamp = () => ({ contextTime: 0, performanceTime: 0 });
	const param = () => ({ value: 0, setTargetAtTime(v) { this.value = v; }, setValueAtTime(v) { this.value = v; } });
	const node = (kind) => ({
		kind,
		context: ctx,
		gain: param(),
		delayTime: param(),
		fftSize: 32,
		connect() { return this; },
		disconnect() {}
	});
	ctx.destination = node('destination');
	ctx.createGain = () => node('gain');
	ctx.createDelay = () => node('delay');
	ctx.createChannelSplitter = () => node('splitter');
	ctx.createChannelMerger = () => node('merger');
	ctx.createAnalyser = () => node('analyser');
	return ctx;
}

async function waitFor(predicate, what) {
	for (let i = 0; !predicate(); i++) {
		if (i >= 500) assert.fail(`timed out waiting for ${what}`);
		await new Promise((r) => setTimeout(r, 1));
	}
}

let hp;
let store;

/** The CUE device id the last persisted mixer config saved, or null. */
function savedCueId() {
	for (const value of store.values()) {
		try {
			const id = JSON.parse(value)?.saved_outputs?.cue?.id;
			if (id !== undefined) return id;
		} catch {
			// not the mixer config
		}
	}
	return null;
}

/** The MAIN device id the last persisted mixer config saved, or null. */
function savedMainId() {
	for (const value of store.values()) {
		try {
			const id = JSON.parse(value)?.saved_outputs?.master?.id;
			if (id !== undefined) return id;
		} catch {
			// not the mixer config
		}
	}
	return null;
}

before(async () => {
	store = new Map();
	globalThis.window = {
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	};
	globalThis.OPENDJ_CUE_SINK = { url: 'ws://127.0.0.1:4321/cue', token: 't' };
	globalThis.Worker = ReplyWorker;
	globalThis.AudioWorkletNode = StubWorkletNode;
	hp = await loadTypeScriptModule('tests/unit/fixtures/headphone-picks-entry.ts');
});

let ctx;
let source;

// The headphone liveness monitor keeps timers; stop them so the run can exit.
after(() => hp.disposeHeadphoneMonitor());

beforeEach(() => {
	holdTypes = [];
	nativeCueOpen = null;
	nativeDevices = null;
	cueCommands.length = 0;
	held.length = 0;
	store.clear();
	hp.disposeHeadphoneMonitor();
	const state = hp.mixerState.headphones;
	state.outputs = [
		{ id: SPEAKERS, label: 'MacBook Pro Speakers' },
		{ id: USB, label: 'USB Interface' }
	];
	state.output_mode = 'practice';
	state.split_reason = null;
	state.selected_output_device_id = null;
	state.selected_master_output_device_id = null;
	state.active = false;
	state.routes.master = { state: 'default', selected: false };
	state.routes.cue = { state: 'default', selected: false };
	state.error = null;
	ctx = roomContext();
	source = () => ({ context: ctx, masterGain: ctx.createGain() });
});

test('control: a split CUE pick with no newer CUE pick publishes and saves its CUE', async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	assert.equal(state.output_mode, 'two_outputs');
	await hp.selectHeadphoneOutput(SPEAKERS, source);
	assert.equal(state.output_mode, 'split_cable');
	assert.equal(state.split_reason, 'same_device');
	assert.equal(savedCueId(), SPEAKERS);
});

test('a newer CUE pick during a split CUE pick\'s MAIN move wins, and the split publishes nothing', async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(USB, source);
	// MAIN's move to the speakers is pending, so the split must pin it itself.
	holdTypes = ['set_master', 'set_master'];
	const main = hp.selectMasterOutput(SPEAKERS, source);
	await waitFor(() => held.length === 1, 'the MAIN pick to send set_master');
	const split = hp.selectHeadphoneOutput(SPEAKERS, source);
	await waitFor(() => held.length === 2, 'the split to send its own set_master');
	// The operator picks the USB interface for CUE instead.
	await hp.selectHeadphoneOutput(USB, source);
	assert.equal(state.selected_output_device_id, USB);
	for (const reply of held) reply.ok();
	await Promise.allSettled([main, split]);
	assert.equal(state.selected_output_device_id, USB, 'the older split must not clear the newer CUE');
	assert.equal(state.output_mode, 'two_outputs', 'the older split must not publish split_cable');
	assert.equal(state.split_reason, null);
	assert.equal(state.routes.cue.state, 'selected');
	assert.equal(savedCueId(), USB);
});

test('a newer CUE pick while a committed split closes the old CUE output keeps its saved CUE', async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	holdTypes = ['close'];
	const split = hp.selectHeadphoneOutput(SPEAKERS, source);
	await waitFor(() => held.length === 1, 'the split to close the old CUE output');
	assert.equal(state.output_mode, 'split_cable', 'precondition: the split committed');
	await hp.selectHeadphoneOutput(USB, source);
	assert.equal(state.output_mode, 'two_outputs');
	held[0].ok();
	await split;
	assert.equal(savedCueId(), USB, 'the older split must not save its CUE over the newer pick');
	assert.equal(state.selected_output_device_id, USB);
	assert.equal(state.output_mode, 'two_outputs');
});

test('an older ordinary CUE pick settling after a newer one publishes, saves and rolls back nothing', async () => {
	const state = hp.mixerState.headphones;
	const BUS = 'native:SecondUsbInterface';
	state.outputs = [...state.outputs, { id: BUS, label: 'Second USB Interface' }];
	await hp.selectMasterOutput(SPEAKERS, source);
	for (const settle of ['ok', 'fail']) {
		await hp.selectHeadphoneOutput(USB, source);
		holdTypes = ['open'];
		const older = hp.selectHeadphoneOutput(BUS, source);
		await waitFor(() => held.length === 1, 'the older pick to open its device');
		await hp.selectHeadphoneOutput(USB, source);
		assert.equal(state.selected_output_device_id, USB);
		if (settle === 'ok') held[0].ok();
		else held[0].fail('open refused');
		await assert.doesNotReject(older, `a superseded pick that settles ${settle} must not reject`);
		assert.equal(state.selected_output_device_id, USB, `${settle}: the older pick must not publish its device`);
		assert.equal(state.routes.cue.state, 'selected', `${settle}: the older pick must not mark the route failed`);
		assert.equal(state.active, true);
		assert.equal(savedCueId(), USB, `${settle}: the older pick must not save its device`);
		held.length = 0;
	}
});

test('a newer CUE pick during a MAIN pick\'s move into a split wins, and the split publishes nothing', async () => {
	const state = hp.mixerState.headphones;
	const BUS = 'native:SecondUsbInterface';
	state.outputs = [...state.outputs, { id: BUS, label: 'Second USB Interface' }];
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	holdTypes = ['set_master'];
	// MAIN onto the CUE's own output: a split that moves MAIN first.
	const split = hp.selectMasterOutput(USB, source);
	await waitFor(() => held.length === 1, 'the split to send set_master');
	await hp.selectHeadphoneOutput(BUS, source);
	assert.equal(state.selected_output_device_id, BUS);
	held[0].ok();
	await split.catch(() => {});
	assert.equal(state.selected_output_device_id, BUS, 'the split must not clear the newer CUE');
	assert.equal(state.output_mode, 'two_outputs', 'the split must not publish split_cable');
	assert.equal(state.split_reason, null);
	assert.equal(savedCueId(), BUS);
});

test('control: re-picking a CUE whose pick is still pending is a no-op that supersedes nothing', async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	const BUS = 'native:SecondUsbInterface';
	state.outputs = [...state.outputs, { id: BUS, label: 'Second USB Interface' }];
	holdTypes = ['open'];
	const pending = hp.selectHeadphoneOutput(BUS, source);
	await waitFor(() => held.length === 1, 'the pick to open its device');
	// The same device again while its pick is pending: a no-op, so it must not retire that pick.
	await hp.selectHeadphoneOutput(BUS, source);
	held[0].ok();
	await pending;
	assert.equal(state.selected_output_device_id, BUS);
	assert.equal(state.routes.cue.state, 'selected', 'the pending pick must still publish its route');
	assert.equal(savedCueId(), BUS, 'the pending pick must still save its device');
});

test('every same-device split checks the CUE serial before it publishes', () => {
	const source = readFileSync(new URL('../../src/lib/player/headphones.ts', import.meta.url), 'utf8');
	const calls = source.split('await _enterSameDeviceSplit(').slice(1).map((rest) => rest.slice(0, rest.indexOf(');')));
	assert.equal(calls.length, 3, 'the refresh, the CUE pick and the MAIN pick each enter a split');
	for (const call of calls) {
		assert.match(call, /(cueSerial|cue\.serial) === _cueSelectSerial|!cueSuperseded\(\)/, `a split call ignores newer CUE picks: ${call}`);
	}
});

/** A browser cue context whose setSinkId calls apply in call order, as Chrome's
 * do. `hold(id)` makes the next call for `id` wait for `release(id, ok)`. */
function browserCueContext() {
	const ctx = roomContext();
	ctx.sinkId = '';
	ctx.sinkCalls = [];
	ctx.close = async () => {
		ctx.state = 'closed';
	};
	let chain = Promise.resolve();
	const holds = new Map();
	ctx.hold = (id) => holds.set(id, null);
	ctx.release = (id, ok = true) => holds.get(id)(ok);
	ctx.isHeld = (id) => typeof holds.get(id) === 'function';
	ctx.setSinkId = (id) => {
		ctx.sinkCalls.push(id);
		const result = chain.then(async () => {
			if (holds.has(id) && holds.get(id) === null) {
				const ok = await new Promise((resolve) => holds.set(id, resolve));
				holds.delete(id);
				if (!ok) throw new Error(`setSinkId ${id} refused`);
			}
			ctx.sinkId = id;
			ctx.afterLand?.(id);
		});
		chain = result.catch(() => {});
		return result;
	};
	return ctx;
}

/** Run `fn` on the browser path: no Mac shell sink, cue contexts from `make`. */
async function onBrowserPath(make, fn) {
	const sink = globalThis.OPENDJ_CUE_SINK;
	const nav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
	const OriginalAudioContext = globalThis.AudioContext;
	delete globalThis.OPENDJ_CUE_SINK;
	Object.defineProperty(globalThis, 'navigator', {
		value: {
			mediaDevices: { enumerateDevices: async () => [], addEventListener() {}, removeEventListener() {} }
		},
		configurable: true
	});
	globalThis.AudioContext = function () {
		return make();
	};
	try {
		await fn();
	} finally {
		// Torn down while this navigator is still installed: teardown unwatches it.
		hp.disposeHeadphoneMonitor();
		globalThis.OPENDJ_CUE_SINK = sink;
		if (nav === undefined) delete globalThis.navigator;
		else Object.defineProperty(globalThis, 'navigator', nav);
		if (OriginalAudioContext === undefined) delete globalThis.AudioContext;
		else globalThis.AudioContext = OriginalAudioContext;
	}
}

const AUX = 'aux-out';
const ROOM = 'room-speakers';
const PHONES = 'usb-phones';
const BUS_OUT = 'second-bus';
const DOCK = 'dock-out';

function browserOutputs(state) {
	state.outputs = [ROOM, PHONES, BUS_OUT, DOCK, AUX].map((id) => ({ id, label: id }));
}

test("an older browser CUE pick's late rollback is put back on the CUE the menu shows (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		assert.equal(cue.sinkId, PHONES);
		cue.hold(BUS_OUT);
		const older = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => cue.isHeld(BUS_OUT), 'the older pick to reach setSinkId');
		const newer = hp.selectHeadphoneOutput(DOCK, source);
		await waitFor(() => cue.sinkCalls.includes(DOCK), 'the newer pick to queue its setSinkId');
		// The older pick fails; its rollback to the phones queues behind the dock.
		cue.release(BUS_OUT, false);
		await Promise.allSettled([older, newer]);
		await waitFor(() => cue.sinkId === DOCK && cue.sinkCalls.at(-1) === DOCK, 'the sink to come back to the dock');
		assert.deepEqual(cue.sinkCalls.slice(-3), [DOCK, PHONES, DOCK], 'the stale rollback landed, then the shown CUE was re-applied');
		assert.equal(state.selected_output_device_id, DOCK);
		assert.equal(state.routes.cue.state, 'selected');
	});
});

test('an older browser CUE pick that resumes the cue context after a newer split is silenced again (browser path)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		assert.equal(cue.state, 'running');
		cue.hold(BUS_OUT);
		const older = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => cue.isHeld(BUS_OUT), 'the older pick to reach setSinkId');
		// CUE onto MAIN's own output: split cue, which silences the cue context.
		await hp.selectHeadphoneOutput(ROOM, source);
		assert.equal(state.output_mode, 'split_cable');
		assert.equal(cue.state, 'suspended');
		cue.release(BUS_OUT, true);
		await older;
		await waitFor(() => cue.state === 'suspended', 'the cue context to be silenced again');
		assert.equal(state.output_mode, 'split_cable');
		assert.equal(state.selected_output_device_id, null);
	});
});

test('control: the newest CUE pick whose rollback cannot restore stays silenced (browser path)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		cue.hold(BUS_OUT);
		cue.hold(PHONES);
		const pick = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => cue.isHeld(BUS_OUT), 'the pick to reach setSinkId');
		cue.release(BUS_OUT, false);
		await waitFor(() => cue.isHeld(PHONES), 'the rollback to reach setSinkId');
		cue.release(PHONES, false);
		await assert.rejects(pick);
		await new Promise((r) => setTimeout(r, 20));
		assert.equal(cue.state, 'suspended', 'a failed restore silences the cue');
		assert.equal(cue.sinkCalls.filter((id) => id === PHONES).length, 2, 'nothing re-applies the CUE over that silence');
	});
});

test('a stale CUE pick waits for the newest one to settle before it corrects the sink (browser path)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		cue.hold(BUS_OUT);
		cue.hold(DOCK);
		const older = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => cue.isHeld(BUS_OUT), 'the older pick to reach setSinkId');
		const newer = hp.selectHeadphoneOutput(DOCK, source);
		await waitFor(() => cue.sinkCalls.includes(DOCK), 'the newer pick to queue its setSinkId');
		cue.release(BUS_OUT, false);
		await waitFor(() => cue.isHeld(DOCK), 'the newer pick to reach setSinkId');
		// The newest pick fails too and rolls the menu back.
		cue.release(DOCK, false);
		await older;
		await assert.rejects(newer);
		await new Promise((r) => setTimeout(r, 20));
		assert.notEqual(cue.sinkId, DOCK, 'the failed newer device must not be re-applied over its rollback');
		assert.equal(cue.sinkId, state.selected_output_device_id, 'the sink ends on the CUE the menu shows');
	});
});

test('a split that supersedes a pending MAIN pick and then fails settles MAIN where the sink landed', async () => {
	const state = hp.mixerState.headphones;
	for (const mainMove of ['lands', 'fails']) {
		hp.disposeHeadphoneMonitor();
		store.clear();
		held.length = 0;
		state.selected_master_output_device_id = null;
		state.selected_output_device_id = null;
		state.output_mode = 'practice';
		state.routes.master = { state: 'default', selected: false };
		await hp.selectMasterOutput(USB, source);
		const suspendsBefore = ctx.suspends;
		holdTypes = ['set_master', 'set_master'];
		const main = hp.selectMasterOutput(SPEAKERS, source);
		await waitFor(() => held.length === 1, 'the MAIN pick to send set_master');
		// CUE onto the MAIN shown (optimistically the speakers): a split that
		// must pin MAIN itself, superseding that pending MAIN pick.
		const split = hp.selectHeadphoneOutput(SPEAKERS, source);
		await waitFor(() => held.length === 2, 'the split to send its own set_master');
		if (mainMove === 'lands') held[0].ok();
		else held[0].fail('set_master refused');
		held[1].fail('split set_master refused');
		await main.catch(() => {});
		await assert.rejects(split);
		const landed = mainMove === 'lands' ? SPEAKERS : USB;
		assert.equal(state.selected_master_output_device_id, landed, `${mainMove}: MAIN shows where the sink landed`);
		assert.equal(state.routes.master.state, 'selected');
		assert.equal(savedMainId(), landed, `${mainMove}: MAIN is saved where it landed`);
		assert.equal(ctx.suspends, suspendsBefore + 1, `${mainMove}: the room is reopened once after the failed split`);
		assert.notEqual(state.output_mode, 'split_cable');
	}
});

test('control: a split with no MAIN pick pending that fails leaves MAIN alone and reopens nothing', async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	// MAIN onto the CUE's output with MAIN already landed elsewhere: the split
	// moves MAIN itself, and that move fails.
	const suspendsBefore = ctx.suspends;
	holdTypes = ['set_master'];
	const split = hp.selectMasterOutput(USB, source);
	await waitFor(() => held.length === 1, 'the split to send set_master');
	held[0].fail('split set_master refused');
	await assert.rejects(split);
	assert.equal(state.selected_master_output_device_id, SPEAKERS);
	assert.equal(ctx.suspends, suspendsBefore, 'nothing reopens the room for a MAIN that never changed');
	assert.equal(savedMainId(), SPEAKERS);
});

test("a newer MAIN pick during that settle's reopen keeps the settle from publishing", async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(USB, source);
	holdTypes = ['set_master', 'set_master'];
	const main = hp.selectMasterOutput(SPEAKERS, source);
	await waitFor(() => held.length === 1, 'the MAIN pick to send set_master');
	const split = hp.selectHeadphoneOutput(SPEAKERS, source);
	await waitFor(() => held.length === 2, 'the split to send its own set_master');
	let finishResume;
	ctx.resume = () => new Promise((resolve) => (finishResume = () => {
		ctx.state = 'running';
		resolve();
	}));
	held[0].ok();
	held[1].fail('split set_master refused');
	await waitFor(() => finishResume !== undefined, 'the settle to reach resume');
	ctx.resume = async () => {
		ctx.state = 'running';
	};
	// The operator picks MAIN again while the settle reopens the room.
	// It lands before the settle's reopen does, so a settle that published
	// after its reopen would save over it.
	await hp.selectMasterOutput(USB, source);
	assert.equal(savedMainId(), USB);
	finishResume();
	await Promise.allSettled([main, split]);
	assert.equal(state.selected_master_output_device_id, USB, 'the newer pick owns MAIN');
	assert.equal(savedMainId(), USB, 'the stale settle must not save over the newer pick');
});

test('a failed split with no landed move since a remount goes back to the saved MAIN', async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(SPEAKERS, source);
	assert.equal(savedMainId(), SPEAKERS);
	// /performance remounts: the routes are rebuilt, the shown MAIN is kept.
	hp.disposeHeadphoneMonitor();
	assert.equal(state.selected_master_output_device_id, SPEAKERS, 'precondition: a remount keeps the shown MAIN');
	holdTypes = ['set_master', 'set_master'];
	const main = hp.selectMasterOutput(USB, source);
	await waitFor(() => held.length === 1, 'the MAIN pick to send set_master');
	const split = hp.selectHeadphoneOutput(USB, source);
	await waitFor(() => held.length === 2, 'the split to send its own set_master');
	setMasterUids.length = 0;
	held[0].fail('set_master refused');
	held[1].fail('split set_master refused');
	await main.catch(() => {});
	await assert.rejects(split);
	assert.equal(state.selected_master_output_device_id, SPEAKERS, 'MAIN must not be cleared');
	assert.deepEqual(setMasterUids, ['BuiltInSpeakerDevice'], 'the saved MAIN is pinned again');
	assert.equal(state.routes.master.state, 'selected');
	assert.equal(savedMainId(), SPEAKERS);
});

test("a CUE pick during the stale-pick correction's move keeps the sink on the newest CUE (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		// The first DOCK landing is the newer pick's; hold the next one, the
		// correction that follows the stale rollback.
		let armed = false;
		cue.afterLand = (id) => {
			if (id === DOCK && !armed) {
				armed = true;
				cue.hold(DOCK);
			}
		};
		cue.hold(BUS_OUT);
		const older = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => cue.isHeld(BUS_OUT), 'the older pick to reach setSinkId');
		const newer = hp.selectHeadphoneOutput(DOCK, source);
		await waitFor(() => cue.sinkCalls.includes(DOCK), 'the newer pick to queue its setSinkId');
		cue.release(BUS_OUT, false);
		await Promise.allSettled([older, newer]);
		await waitFor(() => cue.isHeld(DOCK), 'the correction to reach setSinkId');
		// The operator picks another CUE while the correction is pending, and
		// the correction then fails: its rollback queues behind that pick.
		const latest = hp.selectHeadphoneOutput(AUX, source);
		await waitFor(() => cue.sinkCalls.at(-1) === AUX, 'the latest pick to queue its setSinkId');
		cue.release(DOCK, false);
		await latest;
		await waitFor(() => cue.sinkId === AUX && cue.sinkCalls.at(-1) === AUX && cue.sinkCalls.at(-2) !== AUX, 'the sink to end on the latest CUE');
		await new Promise((r) => setTimeout(r, 20));
		assert.equal(state.selected_output_device_id, AUX);
		assert.equal(cue.sinkId, AUX, 'the correction\'s rollback must not leave the sink on a stale CUE');
		assert.equal(state.error, null, 'a correction the newer pick superseded shows no error over it');
	});
});

test('control: a stale-pick correction that fails with no newer pick is tried once (browser path)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		let armed = false;
		cue.afterLand = (id) => {
			if (id === DOCK && !armed) {
				armed = true;
				cue.hold(DOCK);
			}
		};
		cue.hold(BUS_OUT);
		const older = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => cue.isHeld(BUS_OUT), 'the older pick to reach setSinkId');
		const newer = hp.selectHeadphoneOutput(DOCK, source);
		await waitFor(() => cue.sinkCalls.includes(DOCK), 'the newer pick to queue its setSinkId');
		cue.release(BUS_OUT, false);
		await Promise.allSettled([older, newer]);
		await waitFor(() => cue.isHeld(DOCK), 'the correction to reach setSinkId');
		cue.release(DOCK, false);
		await new Promise((r) => setTimeout(r, 50));
		assert.equal(cue.sinkCalls.filter((id) => id === DOCK).length, 2, 'the pick and one correction, no retry loop');
		assert.equal(state.selected_output_device_id, DOCK);
		assert.match(state.error ?? '', /CUE did not follow the last switch .*Pick the CUE device again/, 'the operator is told to pick again');
	});
});

test("a retired mount's still-pending CUE pick does not hold up the new mount's correction (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const oldCue = browserCueContext();
	const newCue = browserCueContext();
	let made = 0;
	await onBrowserPath(() => (made++ === 0 ? oldCue : newCue), async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		oldCue.hold(BUS_OUT);
		const retired = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => oldCue.isHeld(BUS_OUT), 'the old mount pick to reach setSinkId');
		// /performance remounts while that pick is still pending.
		hp.disposeHeadphoneMonitor();
		assert.equal(hp.outputSwitchesPending().cue, false, "the retired mount's pick is not the new mount's pending work");
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		assert.equal(newCue.sinkId, PHONES, 'the new mount built its own cue context');
		newCue.hold(BUS_OUT);
		const older = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => newCue.isHeld(BUS_OUT), 'the older new-mount pick to reach setSinkId');
		const newer = hp.selectHeadphoneOutput(DOCK, source);
		await waitFor(() => newCue.sinkCalls.includes(DOCK), 'the newer pick to queue its setSinkId');
		// The older pick fails; its rollback to the phones lands after the dock.
		newCue.release(BUS_OUT, false);
		await Promise.allSettled([older, newer]);
		// Corrected while the retired mount's pick is still pending.
		await waitFor(() => newCue.sinkId === DOCK && newCue.sinkCalls.at(-1) === DOCK, 'the new mount sink to come back to the dock');
		assert.deepEqual(newCue.sinkCalls.slice(-3), [DOCK, PHONES, DOCK], 'the stale rollback landed, then the shown CUE was re-applied');
		// The retired mount's pick settles last of all and changes nothing.
		oldCue.release(BUS_OUT, true);
		await retired.catch(() => {});
		assert.equal(newCue.sinkId, DOCK);
		assert.equal(state.selected_output_device_id, DOCK);
		await waitFor(() => !hp.outputSwitchesPending().cue, 'the correction to settle, leaving no CUE work pending');
		assert.equal(newCue.sinkId, DOCK);
	});
});

test("control: a retired mount's CUE pick settling never releases the new mount's pending pick (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const oldCue = browserCueContext();
	const newCue = browserCueContext();
	let made = 0;
	await onBrowserPath(() => (made++ === 0 ? oldCue : newCue), async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		oldCue.hold(BUS_OUT);
		const retired = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => oldCue.isHeld(BUS_OUT), 'the old mount pick to reach setSinkId');
		hp.disposeHeadphoneMonitor();
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		newCue.hold(DOCK);
		const pick = hp.selectHeadphoneOutput(DOCK, source);
		await waitFor(() => newCue.isHeld(DOCK), 'the new mount pick to reach setSinkId');
		oldCue.release(BUS_OUT, true);
		await retired.catch(() => {});
		assert.equal(hp.outputSwitchesPending().cue, true, "the new mount's pick is still switching");
		newCue.release(DOCK, true);
		await pick;
		assert.equal(hp.outputSwitchesPending().cue, false);
	});
});

test("control: a retired mount's superseded CUE pick never re-applies the new mount's silenced CUE (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const oldCue = browserCueContext();
	const newCue = browserCueContext();
	let made = 0;
	await onBrowserPath(() => (made++ === 0 ? oldCue : newCue), async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		oldCue.hold(BUS_OUT);
		const retired = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => oldCue.isHeld(BUS_OUT), 'the old mount pick to reach setSinkId');
		hp.disposeHeadphoneMonitor();
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		// The new mount's only pick fails and its rollback cannot restore: silenced.
		newCue.hold(DOCK);
		newCue.hold(PHONES);
		const pick = hp.selectHeadphoneOutput(DOCK, source);
		await waitFor(() => newCue.isHeld(DOCK), 'the pick to reach setSinkId');
		newCue.release(DOCK, false);
		await waitFor(() => newCue.isHeld(PHONES), 'the rollback to reach setSinkId');
		newCue.release(PHONES, false);
		await assert.rejects(pick);
		assert.equal(newCue.state, 'suspended');
		const calls = newCue.sinkCalls.length;
		// The retired mount's pick, superseded by the new mount's, settles last.
		oldCue.release(BUS_OUT, true);
		await retired.catch(() => {});
		await new Promise((r) => setTimeout(r, 20));
		assert.equal(newCue.sinkCalls.length, calls, "the old mount's staleness corrects nothing on the new one");
		assert.equal(newCue.state, 'suspended', 'the silenced CUE stays silent');
	});
});

/** A browser room context whose setSinkId calls settle independently: `hold`
 * a call key (`id#n`, the nth call for that id) to settle it later with
 * `release(key, ok)`, or `refuse` it outright. */
function independentRoom() {
	const room = roomContext();
	room.sinkId = '';
	room.sinkCalls = [];
	const holds = new Map();
	const refused = new Set();
	room.hold = (key) => holds.set(key, null);
	room.refuse = (key) => refused.add(key);
	room.isHeld = (key) => typeof holds.get(key) === 'function';
	room.release = (key, ok = true) => holds.get(key)(ok);
	room.setSinkId = (id) => {
		room.sinkCalls.push(id);
		const key = `${id}#${room.sinkCalls.filter((c) => c === id).length}`;
		if (refused.has(key)) return Promise.reject(new Error(`setSinkId ${key} refused`));
		if (!holds.has(key)) {
			room.sinkId = id;
			return Promise.resolve();
		}
		return new Promise((resolve, reject) =>
			holds.set(key, (ok) => {
				holds.delete(key);
				if (!ok) return reject(new Error(`setSinkId ${key} refused`));
				room.sinkId = id;
				resolve();
			})
		);
	};
	return room;
}

test("a failed split's fallback move that lands after a newer MAIN pick is moved back to the MAIN shown (browser path)", async () => {
	const state = hp.mixerState.headphones;
	await onBrowserPath(() => browserCueContext(), async () => {
		browserOutputs(state);
		const oldRoom = independentRoom();
		await hp.selectMasterOutput(ROOM, () => ({ context: oldRoom, masterGain: oldRoom.createGain() }));
		assert.equal(savedMainId(), ROOM);
		// A remount forgets where the sink landed, so a failed split falls back to the saved MAIN.
		hp.disposeHeadphoneMonitor();
		const room = independentRoom();
		const roomSource = () => ({ context: room, masterGain: room.createGain() });
		room.hold('dock-out#1');
		room.refuse('dock-out#2');
		room.hold('room-speakers#1');
		const pending = hp.selectMasterOutput(DOCK, roomSource);
		await waitFor(() => room.isHeld('dock-out#1'), 'the MAIN pick to reach setSinkId');
		// CUE onto the optimistic MAIN: a split that supersedes the pending pick and fails its move.
		const split = hp.selectHeadphoneOutput(DOCK, roomSource);
		await waitFor(() => room.isHeld('room-speakers#1'), 'the fallback move to the saved MAIN');
		room.release('dock-out#1', false);
		await pending.catch(() => {});
		const newer = hp.selectMasterOutput(AUX, roomSource);
		await newer;
		assert.equal(state.selected_master_output_device_id, AUX, 'the newer pick published');
		// The fallback lands last.
		room.release('room-speakers#1', true);
		await split.catch(() => {});
		await waitFor(() => room.sinkId === AUX, 'the sink to come back to the MAIN shown');
		assert.equal(state.selected_master_output_device_id, AUX);
		assert.equal(state.routes.master.state, 'selected');
		await waitFor(() => !hp.outputSwitchesPending().master, 'the correction to settle');
	});
});

test("control: a failed split's fallback move with no newer pick settles MAIN on the saved MAIN (browser path)", async () => {
	const state = hp.mixerState.headphones;
	await onBrowserPath(() => browserCueContext(), async () => {
		browserOutputs(state);
		const oldRoom = independentRoom();
		await hp.selectMasterOutput(ROOM, () => ({ context: oldRoom, masterGain: oldRoom.createGain() }));
		hp.disposeHeadphoneMonitor();
		const room = independentRoom();
		const roomSource = () => ({ context: room, masterGain: room.createGain() });
		room.hold('dock-out#1');
		room.refuse('dock-out#2');
		const pending = hp.selectMasterOutput(DOCK, roomSource);
		await waitFor(() => room.isHeld('dock-out#1'), 'the MAIN pick to reach setSinkId');
		const split = hp.selectHeadphoneOutput(DOCK, roomSource);
		await split.catch(() => {});
		room.release('dock-out#1', false);
		await pending.catch(() => {});
		await new Promise((r) => setTimeout(r, 20));
		assert.equal(room.sinkId, ROOM);
		assert.equal(state.selected_master_output_device_id, ROOM);
		assert.equal(room.sinkCalls.filter((id) => id === ROOM).length, 1, 'no correction moves the sink again');
	});
});

test("a split's old-CUE suspend that lands after its timeout does not leave a newer CUE silent (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		assert.equal(cue.state, 'running');
		const suspend = cue.suspend;
		let finishSuspend;
		cue.suspend = () => new Promise((resolve) => (finishSuspend = () => suspend().then(resolve)));
		const flush = async () => {
			for (let i = 0; i < 200; i++) await Promise.resolve();
		};
		mock.timers.enable({ apis: ['setTimeout'] });
		let main;
		try {
			// MAIN onto the CUE's own output: a split whose old-CUE suspend hangs past the timeout.
			main = hp.selectMasterOutput(PHONES, source);
			for (let i = 0; i < 50 && finishSuspend === undefined; i++) await flush();
			assert.notEqual(finishSuspend, undefined, 'the split started silencing the old CUE output');
			cue.suspend = suspend;
			mock.timers.tick(5_000);
			await flush();
		} finally {
			mock.timers.reset();
		}
		await main;
		assert.equal(state.output_mode, 'split_cable', 'precondition: the split committed');
		assert.match(state.error ?? '', /old CUE output did not close/, 'precondition: the close timed out');
		// The context still reads running, so this pick does not resume it.
		await hp.selectHeadphoneOutput(DOCK, source);
		assert.equal(cue.state, 'running');
		finishSuspend();
		await waitFor(() => cue.state === 'running' && cue.sinkId === DOCK, 'the new CUE to sound again');
		await waitFor(() => !hp.outputSwitchesPending().cue, 'every CUE work unit, the late suspend included, to settle');
		assert.equal(cue.state, 'running');
		assert.equal(state.selected_output_device_id, DOCK);
	});
});

/** Settle promise chains while setTimeout is mocked. */
async function flushMicrotasks() {
	for (let i = 0; i < 200; i++) await Promise.resolve();
}

test("the correction's split-silencing suspend that lands after its timeout does not leave a newer CUE silent (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		cue.hold(BUS_OUT);
		const older = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => cue.isHeld(BUS_OUT), 'the older pick to reach setSinkId');
		await hp.selectHeadphoneOutput(ROOM, source);
		assert.equal(state.output_mode, 'split_cable');
		const suspend = cue.suspend;
		let finishSuspend;
		cue.suspend = () => new Promise((resolve) => (finishSuspend = () => suspend().then(resolve)));
		mock.timers.enable({ apis: ['setTimeout'] });
		try {
			cue.release(BUS_OUT, true);
			for (let i = 0; i < 50 && finishSuspend === undefined; i++) await flushMicrotasks();
			assert.notEqual(finishSuspend, undefined, 'the correction started its suspend');
			cue.suspend = suspend;
			mock.timers.tick(5_000);
			await flushMicrotasks();
		} finally {
			mock.timers.reset();
		}
		await older;
		await hp.selectHeadphoneOutput(DOCK, source);
		assert.equal(cue.state, 'running', 'precondition: the timed-out suspend has not landed');
		finishSuspend();
		await waitFor(() => cue.state === 'running' && cue.sinkId === DOCK && !hp.outputSwitchesPending().cue, 'the new CUE to sound again');
		assert.equal(state.selected_output_device_id, DOCK);
	});
});

test("a correction's cue silence that never settles stops counting as pending CUE work after its grace (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		cue.hold(BUS_OUT);
		const older = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => cue.isHeld(BUS_OUT), 'the older pick to reach setSinkId');
		await hp.selectHeadphoneOutput(ROOM, source);
		let started = false;
		cue.suspend = () => {
			started = true;
			return new Promise(() => {});
		};
		mock.timers.enable({ apis: ['setTimeout'] });
		try {
			cue.release(BUS_OUT, true);
			for (let i = 0; i < 50 && !started; i++) await flushMicrotasks();
			assert.ok(started, 'the correction started its suspend');
			mock.timers.tick(5_000);
			await flushMicrotasks();
			assert.equal(hp.outputSwitchesPending().cue, true, 'still held past the operation timeout');
			mock.timers.tick(5_000);
			await flushMicrotasks();
			assert.equal(hp.outputSwitchesPending().cue, false, 'released after the grace');
		} finally {
			mock.timers.reset();
		}
		await older;
	});
});

test("a correction's cue silence that lands after its grace still leaves a newer CUE sounding (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		cue.hold(BUS_OUT);
		const older = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => cue.isHeld(BUS_OUT), 'the older pick to reach setSinkId');
		await hp.selectHeadphoneOutput(ROOM, source);
		const suspend = cue.suspend;
		let finishSuspend;
		cue.suspend = () => new Promise((resolve) => (finishSuspend = () => suspend().then(resolve)));
		mock.timers.enable({ apis: ['setTimeout'] });
		try {
			cue.release(BUS_OUT, true);
			for (let i = 0; i < 50 && finishSuspend === undefined; i++) await flushMicrotasks();
			assert.notEqual(finishSuspend, undefined, 'the correction started its suspend');
			cue.suspend = suspend;
			mock.timers.tick(10_000);
			await flushMicrotasks();
			assert.equal(hp.outputSwitchesPending().cue, false, 'precondition: released after the grace');
		} finally {
			mock.timers.reset();
		}
		await older;
		await hp.selectHeadphoneOutput(DOCK, source);
		assert.equal(cue.state, 'running', 'precondition: the late suspend has not landed');
		finishSuspend();
		await waitFor(() => cue.state === 'running' && cue.sinkId === DOCK && !hp.outputSwitchesPending().cue, 'the new CUE to sound again');
		assert.equal(state.selected_output_device_id, DOCK);
	});
});

test("a cue silence landing after its grace while a newer pick is pending waits for that pick (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		cue.hold(BUS_OUT);
		const older = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => cue.isHeld(BUS_OUT), 'the older pick to reach setSinkId');
		await hp.selectHeadphoneOutput(ROOM, source);
		const suspend = cue.suspend;
		let finishSuspend;
		cue.suspend = () => new Promise((resolve) => (finishSuspend = () => suspend().then(resolve)));
		mock.timers.enable({ apis: ['setTimeout'] });
		try {
			cue.release(BUS_OUT, true);
			for (let i = 0; i < 50 && finishSuspend === undefined; i++) await flushMicrotasks();
			assert.notEqual(finishSuspend, undefined, 'the correction started its suspend');
			cue.suspend = suspend;
			mock.timers.tick(10_000);
			await flushMicrotasks();
		} finally {
			mock.timers.reset();
		}
		await older;
		cue.hold(DOCK);
		const newer = hp.selectHeadphoneOutput(DOCK, source);
		await waitFor(() => cue.isHeld(DOCK), 'the newer pick to reach setSinkId');
		finishSuspend();
		await new Promise((r) => setTimeout(r, 20));
		assert.equal(cue.sinkCalls.filter((id) => id === DOCK).length, 1, 'no correction re-applies the pending device');
		cue.release(DOCK, true);
		await newer;
		await waitFor(() => cue.state === 'running' && cue.sinkId === DOCK && !hp.outputSwitchesPending().cue, 'the newer CUE to sound');
		assert.equal(state.selected_output_device_id, DOCK);
	});
});

test("control: a newest pick's rollback silence that lands after its grace stays silenced (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		cue.hold(BUS_OUT);
		cue.hold(PHONES);
		const pick = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => cue.isHeld(BUS_OUT), 'the pick to reach setSinkId');
		cue.release(BUS_OUT, false);
		await waitFor(() => cue.isHeld(PHONES), 'the rollback to reach setSinkId');
		const suspend = cue.suspend;
		let finishSuspend;
		cue.suspend = () => new Promise((resolve) => (finishSuspend = () => suspend().then(resolve)));
		mock.timers.enable({ apis: ['setTimeout'] });
		try {
			cue.release(PHONES, false);
			for (let i = 0; i < 50 && finishSuspend === undefined; i++) await flushMicrotasks();
			assert.notEqual(finishSuspend, undefined, 'the rollback started silencing the cue');
			cue.suspend = suspend;
			mock.timers.tick(10_000);
			await flushMicrotasks();
		} finally {
			mock.timers.reset();
		}
		await assert.rejects(pick);
		const calls = cue.sinkCalls.length;
		finishSuspend();
		await new Promise((r) => setTimeout(r, 20));
		assert.equal(cue.state, 'suspended', 'the failed restore stays silenced');
		assert.equal(cue.sinkCalls.length, calls, 'nothing re-applies the CUE over that silence');
	});
});

test("a failed pick's rollback silence that lands after its timeout does not leave a newer CUE silent (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		cue.hold(BUS_OUT);
		cue.hold(PHONES);
		const pick = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => cue.isHeld(BUS_OUT), 'the pick to reach setSinkId');
		cue.release(BUS_OUT, false);
		await waitFor(() => cue.isHeld(PHONES), 'the rollback to reach setSinkId');
		const suspend = cue.suspend;
		let finishSuspend;
		cue.suspend = () => new Promise((resolve) => (finishSuspend = () => suspend().then(resolve)));
		mock.timers.enable({ apis: ['setTimeout'] });
		try {
			cue.release(PHONES, false);
			for (let i = 0; i < 50 && finishSuspend === undefined; i++) await flushMicrotasks();
			assert.notEqual(finishSuspend, undefined, 'the rollback started silencing the cue');
			cue.suspend = suspend;
			mock.timers.tick(5_000);
			await flushMicrotasks();
		} finally {
			mock.timers.reset();
		}
		await assert.rejects(pick);
		await hp.selectHeadphoneOutput(DOCK, source);
		assert.equal(cue.state, 'running', 'precondition: the timed-out suspend has not landed');
		finishSuspend();
		await waitFor(() => cue.state === 'running' && cue.sinkId === DOCK && !hp.outputSwitchesPending().cue, 'the new CUE to sound again');
		assert.equal(state.selected_output_device_id, DOCK);
	});
});

test('a CUE pick a teardown retires rolls back nothing over the replacement mount', async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(SPEAKERS, source);
	holdTypes = ['open'];
	const pick = hp.selectHeadphoneOutput(USB, source);
	await waitFor(() => held.length === 1, 'the pick to open its device');
	hp.disposeHeadphoneMonitor();
	// The replacement mount publishes its own CUE before the old pick settles.
	state.selected_output_device_id = SPEAKERS;
	state.output_mode = 'two_outputs';
	state.active = true;
	state.routes.cue = { state: 'selected', selected: true };
	held[0].fail('open refused');
	await assert.rejects(pick, /stale/);
	assert.equal(state.selected_output_device_id, SPEAKERS, 'the stale pick must not roll the new CUE back');
	assert.equal(state.active, true);
	assert.equal(state.output_mode, 'two_outputs');
	assert.equal(state.routes.cue.state, 'selected');
});

test('a MAIN pick that commits a split retires a CUE pick still opening that device', async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(SPEAKERS, source);
	holdTypes = ['open'];
	const cuePick = hp.selectHeadphoneOutput(USB, source);
	await waitFor(() => held.length === 1, 'the CUE pick to open its device');
	assert.equal(state.output_mode, 'two_outputs', 'precondition: the CUE pick shows two outputs');
	// MAIN onto the CUE's own output: split cue, committed while CUE is opening.
	await hp.selectMasterOutput(USB, source);
	assert.equal(state.output_mode, 'split_cable');
	held[0].ok();
	await cuePick;
	assert.equal(state.output_mode, 'split_cable', 'the retired CUE pick must not republish two outputs');
	assert.equal(state.selected_output_device_id, null);
	assert.equal(state.selected_master_output_device_id, USB);
	assert.notEqual(savedCueId(), USB, 'the retired CUE pick must not save its device');
});

test("a CUE pick during the correction's split-silencing suspend still sounds (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		cue.hold(BUS_OUT);
		const older = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => cue.isHeld(BUS_OUT), 'the older pick to reach setSinkId');
		await hp.selectHeadphoneOutput(ROOM, source);
		assert.equal(state.output_mode, 'split_cable');
		// Hold the correction's suspend once the stale pick resumes the context.
		const suspend = cue.suspend;
		let finishSuspend;
		cue.suspend = () => new Promise((resolve) => (finishSuspend = () => suspend().then(resolve)));
		cue.release(BUS_OUT, true);
		await older;
		await waitFor(() => finishSuspend !== undefined, 'the correction to start its suspend');
		cue.suspend = suspend;
		// The operator picks a CUE while that suspend is pending: the context
		// still reads running, so this pick does not resume it.
		await hp.selectHeadphoneOutput(DOCK, source);
		finishSuspend();
		await waitFor(() => cue.state === 'running', 'the new CUE to sound again');
		assert.equal(state.selected_output_device_id, DOCK);
		assert.equal(cue.sinkId, DOCK);
	});
});

test("a CUE pick while a MAIN pick's split is still silencing the old CUE output still sounds (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		assert.equal(cue.state, 'running');
		// MAIN onto the CUE's own output: a split, which then silences the old
		// CUE output. Hold that suspend.
		const suspend = cue.suspend;
		let finishSuspend;
		cue.suspend = () => new Promise((resolve) => (finishSuspend = () => suspend().then(resolve)));
		const main = hp.selectMasterOutput(PHONES, source);
		await waitFor(() => finishSuspend !== undefined, "the split to start silencing the old CUE output");
		cue.suspend = suspend;
		assert.equal(state.output_mode, 'split_cable', 'precondition: the split committed');
		// The context still reads running, so this pick does not resume it.
		await hp.selectHeadphoneOutput(DOCK, source);
		finishSuspend();
		await main;
		await waitFor(() => cue.state === 'running', 'the new CUE to sound again');
		assert.equal(state.selected_output_device_id, DOCK);
		assert.equal(cue.sinkId, DOCK);
	});
});

test('a correction never makes an inactive CUE sound (browser path)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		// No CUE yet: both picks fail, and the newest rolls back to an inactive CUE.
		cue.hold(BUS_OUT);
		cue.hold(DOCK);
		const older = hp.selectHeadphoneOutput(BUS_OUT, source);
		await waitFor(() => cue.isHeld(BUS_OUT), 'the older pick to reach setSinkId');
		const newer = hp.selectHeadphoneOutput(DOCK, source);
		await waitFor(() => cue.sinkCalls.includes(DOCK), 'the newer pick to queue its setSinkId');
		cue.release(BUS_OUT, false);
		await waitFor(() => cue.isHeld(DOCK), 'the newer pick to reach setSinkId');
		cue.release(DOCK, false);
		await Promise.allSettled([older, newer]);
		await new Promise((r) => setTimeout(r, 20));
		assert.equal(state.active, false, 'precondition: the CUE is not playing');
		assert.equal(cue.state, 'suspended', 'an inactive CUE must stay silent');
		assert.deepEqual(cue.sinkCalls, [BUS_OUT, DOCK], 'nothing re-applies a CUE that is not playing');
	});
});

test("a MAIN pick's split that cannot close the old CUE output says it may still sound", async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	holdTypes = ['close'];
	const split = hp.selectMasterOutput(USB, source);
	await waitFor(() => held.length === 1, 'the split to close the old CUE output');
	held[0].fail('close refused');
	await split;
	assert.equal(state.output_mode, 'split_cable');
	assert.match(state.error ?? '', /old CUE output did not close .*may still sound/);
});

test("a MAIN pick's split whose room reopen also fails shows both failures", async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	ctx.resume = async () => {
		throw new Error('resume refused');
	};
	holdTypes = ['close'];
	const split = hp.selectMasterOutput(USB, source);
	await waitFor(() => held.length === 1, 'the split to close the old CUE output');
	held[0].fail('close refused');
	await split.catch(() => {});
	assert.equal(state.output_mode, 'split_cable');
	assert.match(state.error ?? '', /Audio stayed silent after the output change/, 'the room retry is shown');
	assert.match(state.error ?? '', /old CUE output did not close .*may still sound/, 'and so is the old CUE output');
});

test("a CUE pick during a MAIN pick's split reopen moves the sink back to the MAIN shown", async () => {
	const state = hp.mixerState.headphones;
	const BUS = 'native:SecondUsbInterface';
	state.outputs = [...state.outputs, { id: BUS, label: 'Second USB Interface' }];
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	const speakersUid = setMasterUids.at(-1);
	let finishResume;
	ctx.resume = () => new Promise((resolve) => (finishResume = () => {
		ctx.state = 'running';
		resolve();
	}));
	// MAIN onto the CUE's output: the split moves MAIN there, then reopens.
	const split = hp.selectMasterOutput(USB, source);
	await waitFor(() => finishResume !== undefined, 'the split to reach its reopen');
	ctx.resume = async () => {
		ctx.state = 'running';
	};
	assert.notEqual(setMasterUids.at(-1), speakersUid, 'precondition: the split moved MAIN');
	await hp.selectHeadphoneOutput(BUS, source);
	finishResume();
	await split;
	assert.equal(state.selected_master_output_device_id, SPEAKERS, 'the retired split publishes no MAIN');
	assert.equal(state.selected_output_device_id, BUS);
	await waitFor(() => setMasterUids.at(-1) === speakersUid, 'the sink to be moved back to the MAIN shown');
});

test("control: a MAIN pick's split that stays current keeps MAIN on the split device", async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	const before = setMasterUids.length;
	await hp.selectMasterOutput(USB, source);
	assert.equal(state.output_mode, 'split_cable');
	assert.equal(state.selected_master_output_device_id, USB);
	assert.equal(setMasterUids.length, before + 1, 'one move, no correction');
});

test("a MAIN pick's split move that fails after it was retired shows no error", async () => {
	const state = hp.mixerState.headphones;
	const BUS = 'native:SecondUsbInterface';
	state.outputs = [...state.outputs, { id: BUS, label: 'Second USB Interface' }];
	for (const retiredBy of ['teardown', 'cue pick']) {
		hp.disposeHeadphoneMonitor();
		held.length = 0;
		state.output_mode = 'practice';
		state.selected_output_device_id = null;
		state.error = null;
		await hp.selectMasterOutput(SPEAKERS, source);
		await hp.selectHeadphoneOutput(USB, source);
		holdTypes = ['set_master'];
		const split = hp.selectMasterOutput(USB, source);
		await waitFor(() => held.length === 1, 'the split to send set_master');
		if (retiredBy === 'teardown') {
			hp.disposeHeadphoneMonitor();
			state.error = 'the replacement mount error';
		} else {
			await hp.selectHeadphoneOutput(BUS, source);
			state.error = 'the newer CUE pick error';
		}
		const shown = state.error;
		held[0].fail('split set_master refused');
		await split;
		assert.equal(state.error, shown, `${retiredBy}: the retired split must not show its failure`);
	}
});

test("control: a MAIN pick's split move that fails while current is shown and rejects", async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	holdTypes = ['set_master'];
	const split = hp.selectMasterOutput(USB, source);
	await waitFor(() => held.length === 1, 'the split to send set_master');
	held[0].fail('split set_master refused');
	await assert.rejects(split);
	assert.match(state.error ?? '', /Could not split MAIN and CUE/);
});

test('a MAIN move that lands after a teardown is not trusted as landed by the next mount (browser path)', async () => {
	const state = hp.mixerState.headphones;
	await onBrowserPath(() => browserCueContext(), async () => {
		browserOutputs(state);
		const oldRoom = browserCueContext();
		const oldSource = () => ({ context: oldRoom, masterGain: oldRoom.createGain() });
		await hp.selectMasterOutput(ROOM, oldSource);
		assert.equal(savedMainId(), ROOM);
		oldRoom.hold(PHONES);
		const stale = hp.selectMasterOutput(PHONES, oldSource);
		await waitFor(() => oldRoom.isHeld(PHONES), 'the old mount pick to reach setSinkId');
		// /performance unmounts; the old room's move lands only afterwards.
		hp.disposeHeadphoneMonitor();
		oldRoom.release(PHONES, true);
		await stale.catch(() => {});
		const newRoom = browserCueContext();
		const newSource = () => ({ context: newRoom, masterGain: newRoom.createGain() });
		const queue = newRoom.setSinkId;
		let dockCalls = 0;
		newRoom.setSinkId = (id) => {
			if (id === DOCK && ++dockCalls === 2) {
				newRoom.sinkCalls.push(id);
				return Promise.reject(new Error('split setSinkId refused'));
			}
			return queue(id);
		};
		newRoom.hold(DOCK);
		const main = hp.selectMasterOutput(DOCK, newSource);
		await waitFor(() => newRoom.isHeld(DOCK), 'the new mount MAIN pick to reach setSinkId');
		// CUE onto the optimistic MAIN: a split that supersedes the pending pick.
		const split = hp.selectHeadphoneOutput(DOCK, newSource);
		await waitFor(() => dockCalls === 2, 'the split to try its own move');
		newRoom.release(DOCK, false);
		await main.catch(() => {});
		await split.catch(() => {});
		assert.notEqual(state.selected_master_output_device_id, PHONES, 'the new room never reached the old mount device');
		assert.equal(state.selected_master_output_device_id, ROOM, 'MAIN settles on the saved MAIN');
		assert.equal(newRoom.sinkId, ROOM, 'the new room is pinned on the MAIN shown');
	});
});

test("a CUE pick that fails while an older one is still opening rolls back to the CUE last published", async () => {
	const state = hp.mixerState.headphones;
	const BUS = 'native:SecondUsbInterface';
	const DOCK = 'native:DockOut';
	state.outputs = [...state.outputs, { id: BUS, label: 'Second USB Interface' }, { id: DOCK, label: 'Dock' }];
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(BUS, source);
	holdTypes = ['open', 'open'];
	const older = hp.selectHeadphoneOutput(USB, source);
	await waitFor(() => held.length === 1, 'the older pick to open its device');
	const newer = hp.selectHeadphoneOutput(DOCK, source);
	await waitFor(() => held.length === 2, 'the newer pick to open its device');
	held[1].fail('dock open refused');
	await assert.rejects(newer);
	assert.equal(state.selected_output_device_id, BUS, "the menu must not land on the older pick's unopened device");
	assert.equal(state.routes.cue.state, 'failed');
	held[0].fail('usb open refused');
	await older;
	assert.equal(state.selected_output_device_id, BUS);
	assert.equal(state.output_mode, 'two_outputs');
});

test('control: a CUE pick that fails after a split committed under overlapping picks rolls back to the split', async () => {
	const state = hp.mixerState.headphones;
	const DOCK = 'native:DockOut';
	state.outputs = [...state.outputs, { id: DOCK, label: 'Dock' }];
	await hp.selectMasterOutput(SPEAKERS, source);
	holdTypes = ['open'];
	const older = hp.selectHeadphoneOutput(USB, source);
	await waitFor(() => held.length === 1, 'the older pick to open its device');
	// MAIN onto the CUE shown: split cue, committed while the older pick opens.
	await hp.selectMasterOutput(USB, source);
	assert.equal(state.output_mode, 'split_cable');
	holdTypes = ['open'];
	const newer = hp.selectHeadphoneOutput(DOCK, source);
	await waitFor(() => held.length === 2, 'the newer pick to open its device');
	held[1].fail('dock open refused');
	await assert.rejects(newer);
	assert.equal(state.output_mode, 'split_cable', 'the rollback keeps the split that committed');
	assert.equal(state.selected_output_device_id, null);
	held[0].ok();
	await older;
	assert.equal(state.output_mode, 'split_cable');
	assert.equal(state.selected_master_output_device_id, USB);
});

test('a CUE pick that fails after a newer pick landed, with an older one still opening, rolls back to the one that landed', async () => {
	const state = hp.mixerState.headphones;
	const BUS = 'native:SecondUsbInterface';
	const DOCK = 'native:DockOut';
	state.outputs = [...state.outputs, { id: BUS, label: 'Second USB Interface' }, { id: DOCK, label: 'Dock' }];
	await hp.selectMasterOutput(SPEAKERS, source);
	holdTypes = ['open'];
	const older = hp.selectHeadphoneOutput(USB, source);
	await waitFor(() => held.length === 1, 'the older pick to open its device');
	await hp.selectHeadphoneOutput(BUS, source);
	assert.equal(state.routes.cue.state, 'selected', 'precondition: the newer pick landed');
	holdTypes = ['open'];
	const latest = hp.selectHeadphoneOutput(DOCK, source);
	await waitFor(() => held.length === 2, 'the latest pick to open its device');
	held[1].fail('dock open refused');
	await assert.rejects(latest);
	assert.equal(state.selected_output_device_id, BUS, 'back to the CUE that landed');
	held[0].fail('usb open refused');
	await older;
	assert.equal(state.selected_output_device_id, BUS);
});

test("a CUE pick on a new mount rolls back to that mount's CUE, never one the torn-down mount noted", async () => {
	const state = hp.mixerState.headphones;
	const BUS = 'native:SecondUsbInterface';
	const DOCK = 'native:DockOut';
	state.outputs = [...state.outputs, { id: BUS, label: 'Second USB Interface' }, { id: DOCK, label: 'Dock' }];
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(BUS, source);
	holdTypes = ['open'];
	const retired = hp.selectHeadphoneOutput(USB, source);
	await waitFor(() => held.length === 1, 'the pick to open its device');
	hp.disposeHeadphoneMonitor();
	// The replacement mount shows no CUE (One Output) when the operator picks.
	state.selected_output_device_id = null;
	state.active = false;
	state.output_mode = 'practice';
	state.routes.cue = { state: 'default', selected: false };
	holdTypes = ['open'];
	const pick = hp.selectHeadphoneOutput(DOCK, source);
	await waitFor(() => held.length === 2, 'the new mount pick to open its device');
	held[1].fail('dock open refused');
	await assert.rejects(pick);
	assert.equal(state.selected_output_device_id, null, "the old mount's CUE must not come back");
	assert.equal(state.output_mode, 'practice');
	held[0].fail('usb open refused');
	await retired.catch(() => {});
});

/** Point the browser navigator at `ids` as the audio outputs a listing finds. */
function listOutputs(ids) {
	globalThis.navigator.mediaDevices.enumerateDevices = async () =>
		ids.map((id) => ({ kind: 'audiooutput', deviceId: id, label: id, groupId: id }));
}

// [if] a device change restores a remembered CUE while its sink move is still
// pending, the Audio I/O Test must see a CUE switch pending, or it beeps on an
// output that never opened and reports the shown CUE as sounding.
test('a remembered CUE a device change restores is pending output work until its sink lands (browser path)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		listOutputs([ROOM, DOCK]);
		await hp.refreshHeadphoneOutputs(source);
		assert.equal(state.selected_output_device_id, null, 'precondition: the unplugged CUE is cleared');
		cue.hold(PHONES);
		listOutputs([ROOM, PHONES, DOCK]);
		const refresh = hp.refreshHeadphoneOutputs(source);
		await waitFor(() => cue.isHeld(PHONES), 'the restored CUE to reach setSinkId');
		assert.equal(state.selected_output_device_id, PHONES, 'precondition: the menu already shows the restored CUE');
		assert.equal(hp.outputSwitchesPending().cue, true, 'the restore is pending until the sink lands');
		state.liveness_verdict = null;
		cue.release(PHONES, true);
		await refresh;
		assert.equal(hp.outputSwitchesPending().cue, false, 'control: a landed restore is no longer pending');
		assert.equal(cue.sinkId, PHONES);
		assert.equal(state.liveness_verdict, 'idle', 'control: a landed restore watches its output again');
	});
});

// [if] a device change moves MAIN while that sink move is still pending, the
// Test must see a MAIN switch pending.
test('a MAIN a device change re-pins is pending output work until its sink lands (browser path)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		let landMain;
		const mainCalls = [];
		ctx.setSinkId = async (id) => {
			mainCalls.push(id);
			if (mainCalls.length > 1) await new Promise((resolve) => (landMain = resolve));
		};
		await hp.selectMasterOutput(DOCK, source);
		listOutputs([ROOM, PHONES]);
		const refresh = hp.refreshHeadphoneOutputs(source);
		await waitFor(() => landMain !== undefined, 'the re-pinned MAIN to reach setSinkId');
		assert.notEqual(state.selected_master_output_device_id, DOCK, 'precondition: MAIN moved off the unplugged dock');
		assert.equal(hp.outputSwitchesPending().master, true, 'the re-pin is pending until the sink lands');
		landMain();
		await refresh;
		assert.equal(hp.outputSwitchesPending().master, false, 'control: a landed re-pin is no longer pending');
	});
});

// [if] a device change's MAIN re-pin has landed its sink and is reopening the
// room [then] MAIN still counts as switching until that reopen settles
test('a MAIN re-pin stays pending output work through its room reopen (browser path, Codex/Sol P2 on #6835)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(DOCK, source);
		const resume = ctx.resume;
		let landResume;
		ctx.resume = () => new Promise((resolve) => (landResume = () => resume.call(ctx).then(resolve)));
		listOutputs([ROOM, PHONES]);
		const refresh = hp.refreshHeadphoneOutputs(source);
		await waitFor(() => landResume !== undefined, "the re-pin's reopen to reach resume");
		assert.equal(hp.outputSwitchesPending().master, true, 'MAIN is still switching while the room reopens');
		landResume();
		await refresh;
		ctx.resume = resume;
		assert.equal(hp.outputSwitchesPending().master, false, 'control: a settled reopen leaves nothing pending');
	});
});

// [if] a device change re-pins MAIN, the room is reopened like a pick's:
// repeated sink moves are what leave it `running` on a dead stream.
test('a MAIN a device change re-pins reopens the room, and a failed reopen shows the retry error (browser path)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		for (const fails of [false, true]) {
			browserOutputs(state);
			ctx.setSinkId = async () => {};
			await hp.selectMasterOutput(DOCK, source);
			state.error = null;
			const suspendsBefore = ctx.suspends;
			const resume = ctx.resume;
			if (fails) ctx.resume = async () => { throw new Error('resume died'); };
			listOutputs([ROOM, PHONES]);
			try {
				await hp.refreshHeadphoneOutputs(source);
			} finally {
				ctx.resume = resume;
			}
			assert.notEqual(state.selected_master_output_device_id, DOCK, 'precondition: MAIN moved off the unplugged dock');
			assert.equal(ctx.suspends, suspendsBefore + 1, `fails=${fails}: the re-pin reopens the room once`);
			assert.equal(state.routes.master.state, 'selected', `fails=${fails}: the re-pinned MAIN is published`);
			if (fails) assert.match(state.error ?? '', /resume died.*Pick the device again/);
			else assert.equal(state.error, null, 'control: a clean reopen shows no error');
			await ctx.resume();
		}
	});
});

// [if] a MAIN pick lands while a re-pin reopens the room, the re-pin's late
// reopen failure must not overwrite that pick's result.
test("control: a re-pin a MAIN pick supersedes during its reopen shows nothing (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(DOCK, source);
		const resume = ctx.resume;
		let failResume;
		ctx.resume = () => new Promise((_, reject) => (failResume = () => reject(new Error('resume died'))));
		listOutputs([ROOM, PHONES]);
		const refresh = hp.refreshHeadphoneOutputs(source);
		await waitFor(() => failResume !== undefined, "the re-pin's reopen to reach resume");
		ctx.resume = resume;
		await hp.selectMasterOutput(PHONES, source);
		assert.equal(state.error, null, 'precondition: the pick landed clean');
		failResume();
		await refresh;
		assert.equal(state.selected_master_output_device_id, PHONES);
		assert.equal(state.routes.master.state, 'selected');
		assert.equal(state.error, null, "the superseded re-pin's reopen failure is not shown over the pick");
		await ctx.resume();
	});
});

// [if] a teardown retires a re-pin still in flight, its late landing must not
// take the new mount's pending count below zero.
test("control: a retired mount's late re-pin never leaves the new mount's count negative (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		listOutputs([ROOM, DOCK]);
		await hp.refreshHeadphoneOutputs(source);
		cue.hold(PHONES);
		listOutputs([ROOM, PHONES, DOCK]);
		const refresh = hp.refreshHeadphoneOutputs(source);
		await waitFor(() => cue.isHeld(PHONES), 'the restored CUE to reach setSinkId');
		hp.disposeHeadphoneMonitor();
		assert.equal(hp.outputSwitchesPending().cue, false, 'a teardown drops the retired re-pin');
		cue.release(PHONES, true);
		await refresh.catch(() => {});
		// The new mount's own restore must still read pending.
		browserOutputs(state);
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(DOCK, source);
		listOutputs([ROOM, PHONES]);
		await hp.refreshHeadphoneOutputs(source);
		cue.hold(DOCK);
		listOutputs([ROOM, PHONES, DOCK]);
		const restore = hp.refreshHeadphoneOutputs(source);
		await waitFor(() => cue.isHeld(DOCK), "the new mount's restore to reach setSinkId");
		assert.equal(hp.outputSwitchesPending().cue, true, "the new mount's restore still reads pending");
		cue.release(DOCK, true);
		await restore;
	});
});

// [if] a CUE pick lands while a device change's CUE restore is still pending,
// the restore landing last must not leave the sink on the restored device
// while the menu shows the newer pick.
test("a CUE pick made while a device change's restore is pending wins, and the late restore is corrected (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	// Calls settle independently here, so the newer pick can land first, as it
	// does when the restore is still opening its bridge when the pick starts.
	const ordered = cue.setSinkId;
	cue.setSinkId = (id) => {
		if (!cue.isHeld(id) && !cue.holdsPending?.has(id)) {
			cue.sinkCalls.push(id);
			cue.sinkId = id;
			cue.afterLand?.(id);
			return Promise.resolve();
		}
		return ordered(id);
	};
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		listOutputs([ROOM, DOCK]);
		await hp.refreshHeadphoneOutputs(source);
		cue.holdsPending = new Set([PHONES]);
		cue.hold(PHONES);
		listOutputs([ROOM, PHONES, DOCK]);
		const refresh = hp.refreshHeadphoneOutputs(source);
		await waitFor(() => cue.isHeld(PHONES), 'the restore to reach setSinkId');
		cue.holdsPending.clear();
		const pick = hp.selectHeadphoneOutput(DOCK, source);
		await waitFor(() => cue.sinkId === DOCK, 'the newer pick to land first');
		cue.release(PHONES, true);
		await Promise.allSettled([refresh, pick]);
		await waitFor(() => !hp.outputSwitchesPending().cue, 'every CUE work unit to settle');
		assert.equal(state.selected_output_device_id, DOCK, 'the menu keeps the newer pick');
		assert.equal(cue.sinkId, DOCK, 'the sink follows the menu, not the late restore');
		assert.equal(state.error, null);
	});
});

// [if] a MAIN pick lands while a device change's MAIN re-pin is still pending,
// the re-pin landing last must be moved back to the MAIN shown.
test("a MAIN pick made while a device change's re-pin is pending wins, and the late re-pin is corrected (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		const holds = [];
		ctx.sinkId = '';
		ctx.setSinkId = (id) => {
			if (ctx.holdNext) {
				ctx.holdNext = false;
				return new Promise((resolve) => holds.push(() => { ctx.sinkId = id; resolve(); }));
			}
			ctx.sinkId = id;
			return Promise.resolve();
		};
		await hp.selectMasterOutput(DOCK, source);
		ctx.holdNext = true;
		listOutputs([ROOM, PHONES]);
		const refresh = hp.refreshHeadphoneOutputs(source);
		await waitFor(() => holds.length === 1, 'the re-pin to reach setSinkId');
		const repinTarget = state.selected_master_output_device_id;
		const picked = repinTarget === ROOM ? PHONES : ROOM;
		await hp.selectMasterOutput(picked, source);
		assert.equal(ctx.sinkId, picked, 'precondition: the newer pick landed first');
		holds[0]();
		await refresh;
		await waitFor(() => !hp.outputSwitchesPending().master, 'the correction to settle');
		assert.equal(state.selected_master_output_device_id, picked);
		assert.equal(ctx.sinkId, picked, 'the late re-pin is moved back to the MAIN shown');
		assert.equal(state.routes.master.state, 'selected');
	});
});

// [if] the newer pick is a split (CUE onto MAIN's device), the late restore
// must not switch the second output back on.
test("control: a split picked while a device change's restore is pending stays a split (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		listOutputs([ROOM, DOCK]);
		await hp.refreshHeadphoneOutputs(source);
		cue.hold(PHONES);
		listOutputs([ROOM, PHONES, DOCK]);
		const refresh = hp.refreshHeadphoneOutputs(source);
		await waitFor(() => cue.isHeld(PHONES), 'the restore to reach setSinkId');
		await hp.selectHeadphoneOutput(ROOM, source);
		assert.equal(state.output_mode, 'split_cable', 'precondition: the split published');
		const verdict = state.liveness_verdict;
		cue.release(PHONES, true);
		await refresh.catch(() => {});
		await waitFor(() => !hp.outputSwitchesPending().cue, 'every CUE work unit to settle');
		assert.equal(state.output_mode, 'split_cable');
		assert.equal(state.active, false, 'the late restore never switches the second output back on');
		assert.equal(state.liveness_verdict, verdict, 'nor restarts its output watch');
		assert.equal(cue.state, 'suspended');
	});
});


// [if] an older native CUE open lands after a newer pick's, the shell's CUE
// must be put back on the device the menu shows, not left on the older one.
test('an older native CUE open landing after a newer pick is reopened on the CUE shown', async () => {
	const state = hp.mixerState.headphones;
	const BUS = 'native:SecondUsbInterface';
	state.outputs = [...state.outputs, { id: BUS, label: 'Second USB Interface' }];
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	holdTypes = ['open'];
	const older = hp.selectHeadphoneOutput(BUS, source);
	await waitFor(() => held.length === 1, 'the older pick to open its device');
	await hp.selectHeadphoneOutput(USB, source);
	assert.equal(nativeCueOpen, 'UsbInterface', 'precondition: the newer pick opened first');
	const sent = cueCommands.length;
	held[0].ok();
	await older;
	await waitFor(() => !hp.outputSwitchesPending().cue, 'the correction to settle');
	assert.equal(state.selected_output_device_id, USB);
	assert.equal(nativeCueOpen, 'UsbInterface', 'the shell CUE is back on the CUE shown');
	assert.deepEqual(cueCommands.slice(sent), ['open'], 'one reopen corrects it');
	// Control: with the CUE already on the device shown, a correction sends nothing.
	await new Promise((r) => setTimeout(r, 20));
	assert.deepEqual(cueCommands.slice(sent), ['open']);
});

// [if] the overshoot reopened a native CUE the menu shows as a split, the
// second output would sound behind the split.
test('control: a native CUE closed for a split stays closed when an older open lands late', async () => {
	const state = hp.mixerState.headphones;
	const BUS = 'native:SecondUsbInterface';
	state.outputs = [...state.outputs, { id: BUS, label: 'Second USB Interface' }];
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	holdTypes = ['open'];
	const older = hp.selectHeadphoneOutput(BUS, source);
	await waitFor(() => held.length === 1, 'the older pick to open its device');
	await hp.selectHeadphoneOutput(SPEAKERS, source);
	assert.equal(state.output_mode, 'split_cable', 'precondition: the split committed');
	held[0].ok();
	await older;
	await waitFor(() => !hp.outputSwitchesPending().cue, 'the correction to settle');
	assert.equal(state.output_mode, 'split_cable');
	assert.equal(nativeCueOpen, null, 'the late open is closed again behind the split');
	// The close is a landing too: picking the late open's device again must open it.
	await hp.selectHeadphoneOutput(BUS, source);
	assert.equal(nativeCueOpen, 'SecondUsbInterface', 'the device the late open landed on opens again');
});

// [if] a split's native close forgot it closed the CUE, picking that CUE again
// would take the no-op path and leave the headphones silent.
test("control: picking a CUE again after a split closed it reopens it (native)", async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	await hp.selectHeadphoneOutput(SPEAKERS, source);
	assert.equal(state.output_mode, 'split_cable');
	assert.equal(nativeCueOpen, null, 'precondition: the split closed the CUE');
	await hp.selectHeadphoneOutput(USB, source);
	assert.equal(state.output_mode, 'two_outputs');
	assert.equal(nativeCueOpen, 'UsbInterface');
});

// [if] a stale native CUE pick fails after a newer one already put the CUE
// right, the correction must find nothing to do: no reopen, no extra close.
test('control: a stale native CUE pick that fails late sends no correction when the CUE is already right', async () => {
	const state = hp.mixerState.headphones;
	const BUS = 'native:SecondUsbInterface';
	state.outputs = [...state.outputs, { id: BUS, label: 'Second USB Interface' }];
	await hp.selectMasterOutput(SPEAKERS, source);
	for (const newer of [USB, SPEAKERS]) {
		await hp.selectHeadphoneOutput(USB, source);
		holdTypes = ['open'];
		const older = hp.selectHeadphoneOutput(BUS, source);
		await waitFor(() => held.length === 1, 'the older pick to open its device');
		await hp.selectHeadphoneOutput(newer, source);
		const sent = cueCommands.length;
		const where = nativeCueOpen;
		held[0].fail('open refused');
		await older;
		await waitFor(() => !hp.outputSwitchesPending().cue, 'the stale pick to settle');
		await new Promise((r) => setTimeout(r, 20));
		assert.deepEqual(cueCommands.slice(sent), [], `newer ${newer}: nothing to correct`);
		assert.equal(nativeCueOpen, where);
		held.length = 0;
	}
});

// [if] a correction's native close lands after a newer pick reopened the CUE,
// the CUE must be reopened, not left closed while the menu shows it playing.
test("a correction's native close landing after a newer CUE pick is reopened on the CUE shown", async () => {
	const state = hp.mixerState.headphones;
	const BUS = 'native:SecondUsbInterface';
	state.outputs = [...state.outputs, { id: BUS, label: 'Second USB Interface' }];
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	holdTypes = ['open'];
	const older = hp.selectHeadphoneOutput(BUS, source);
	await waitFor(() => held.length === 1, 'the older pick to open its device');
	await hp.selectHeadphoneOutput(SPEAKERS, source);
	assert.equal(state.output_mode, 'split_cable', 'precondition: the split committed');
	// The late open lands behind the split; its correction's close is held.
	holdTypes = ['close'];
	held[0].ok();
	await older;
	await waitFor(() => held.length === 2, "the correction's close to be sent");
	// A newer CUE pick lands before that close does.
	await hp.selectHeadphoneOutput(USB, source);
	assert.equal(nativeCueOpen, 'UsbInterface', 'precondition: the newer pick opened first');
	held[1].ok();
	await waitFor(() => !hp.outputSwitchesPending().cue, 'every CUE work unit to settle');
	await waitFor(() => nativeCueOpen === 'UsbInterface', 'the CUE to be reopened');
	assert.equal(state.selected_output_device_id, USB);
	assert.equal(state.active, true);
});

// [if] the CUE vanishes and its suspend outlives the timeout, then lands after
// a newer CUE pick opened, that CUE must sound again.
test("a vanished CUE's suspend that lands after its timeout does not leave a newer CUE silent (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		assert.equal(cue.state, 'running');
		const suspend = cue.suspend;
		let finishSuspend;
		cue.suspend = () => new Promise((resolve) => (finishSuspend = () => suspend().then(resolve)));
		listOutputs([ROOM, DOCK]);
		mock.timers.enable({ apis: ['setTimeout'] });
		let refresh;
		try {
			refresh = hp.refreshHeadphoneOutputs(source).catch((error) => error);
			for (let i = 0; i < 50 && finishSuspend === undefined; i++) await flushMicrotasks();
			assert.notEqual(finishSuspend, undefined, 'the vanished CUE started its suspend');
			cue.suspend = suspend;
			mock.timers.tick(5_000);
			await flushMicrotasks();
		} finally {
			mock.timers.reset();
		}
		await refresh;
		assert.equal(state.selected_output_device_id, null, 'precondition: the vanished CUE is cleared');
		await hp.selectHeadphoneOutput(DOCK, source);
		assert.equal(cue.state, 'running', 'precondition: the timed-out suspend has not landed');
		finishSuspend();
		await waitFor(() => cue.state === 'running' && cue.sinkId === DOCK && !hp.outputSwitchesPending().cue, 'the new CUE to sound again');
		assert.equal(state.selected_output_device_id, DOCK);
	});
});

// [if] two device changes re-pin MAIN and the older re-pin lands after the
// newer one, the sink must end on the MAIN the menu shows.
test("an older device-change re-pin landing after a newer one is moved back to the MAIN shown (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		const holds = [];
		ctx.sinkId = '';
		ctx.setSinkId = (id) => {
			if (ctx.holdNext) {
				ctx.holdNext = false;
				return new Promise((resolve) => holds.push(() => { ctx.sinkId = id; resolve(); }));
			}
			ctx.sinkId = id;
			return Promise.resolve();
		};
		await hp.selectMasterOutput(DOCK, source);
		ctx.holdNext = true;
		listOutputs([ROOM, PHONES]);
		const older = hp.refreshHeadphoneOutputs(source);
		await waitFor(() => holds.length === 1, 'the older re-pin to reach setSinkId');
		assert.equal(state.selected_master_output_device_id, ROOM, 'precondition: the older re-pin moved MAIN to the room');
		// The room unplugs in turn: a newer re-pin moves MAIN to other speakers.
		const newerTarget = 'desk-speakers';
		listOutputs([newerTarget, AUX]);
		await hp.refreshHeadphoneOutputs(source);
		assert.equal(state.selected_master_output_device_id, newerTarget, 'precondition: the newer re-pin moved the menu');
		assert.equal(ctx.sinkId, newerTarget, 'precondition: the newer re-pin landed first');
		holds[0]();
		await older;
		await waitFor(() => !hp.outputSwitchesPending().master, 'the correction to settle');
		assert.equal(state.selected_master_output_device_id, newerTarget);
		assert.equal(ctx.sinkId, newerTarget, 'the older re-pin is moved back to the MAIN shown');
	});
});

// [if] one device change silences the vanished CUE and the next restores it
// before that suspend lands, the restored CUE must not be left silent.
test("a CUE a device change restores while the earlier vanish's suspend is landing still sounds (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		const suspend = cue.suspend;
		let finishSuspend;
		cue.suspend = () => new Promise((resolve) => (finishSuspend = () => suspend().then(resolve)));
		listOutputs([ROOM, DOCK]);
		const vanish = hp.refreshHeadphoneOutputs(source).catch((error) => error);
		await waitFor(() => finishSuspend !== undefined, 'the vanished CUE to start its suspend');
		cue.suspend = suspend;
		listOutputs([ROOM, PHONES, DOCK]);
		await hp.refreshHeadphoneOutputs(source);
		assert.equal(state.selected_output_device_id, PHONES, 'precondition: the CUE is restored');
		assert.equal(state.active, true);
		finishSuspend();
		await vanish;
		await waitFor(() => cue.state === 'running' && !hp.outputSwitchesPending().cue, 'the restored CUE to sound again');
		assert.equal(cue.sinkId, PHONES);
	});
});

// [if] a slow device listing returns after a newer one, its snapshot must not
// move the routes back.
test('an older device listing that returns after a newer one is dropped (browser path)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		const mainCalls = [];
		ctx.setSinkId = async (id) => {
			mainCalls.push(id);
		};
		await hp.selectMasterOutput(DOCK, source);
		mainCalls.length = 0;
		const listing = (ids) => ids.map((id) => ({ kind: 'audiooutput', deviceId: id, label: id, groupId: id }));
		let returnStale;
		globalThis.navigator.mediaDevices.enumerateDevices = () => new Promise((resolve) => (returnStale = () => resolve(listing([ROOM, PHONES]))));
		const older = hp.refreshHeadphoneOutputs(source);
		await waitFor(() => returnStale !== undefined, 'the older listing to be requested');
		listOutputs([ROOM, PHONES, DOCK, 'desk-speakers']);
		await hp.refreshHeadphoneOutputs(source);
		assert.equal(state.selected_master_output_device_id, DOCK, 'precondition: the newer listing keeps MAIN');
		assert.ok(state.outputs.some((output) => output.id === 'desk-speakers'), 'control: the newer listing is published');
		returnStale();
		await older;
		assert.equal(state.selected_master_output_device_id, DOCK, 'the stale listing does not move MAIN');
		assert.deepEqual(mainCalls, [], 'nor the sink');
		assert.ok(state.outputs.some((output) => output.id === 'desk-speakers'), 'the newer listing stays published');
	});
});

test('an older device listing that fails after a newer one succeeded shows no failure (browser path)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		let failStale;
		globalThis.navigator.mediaDevices.enumerateDevices = () => new Promise((_, reject) => (failStale = () => reject(new Error('stale listing failed'))));
		const older = hp.refreshHeadphoneOutputs(source).catch((error) => error);
		await waitFor(() => failStale !== undefined, 'the older listing to be requested');
		listOutputs([ROOM, PHONES, DOCK]);
		await hp.refreshHeadphoneOutputs(source);
		const access = state.device_access;
		assert.notEqual(access.status, 'enumeration_failed', 'precondition: the newer listing succeeded');
		failStale();
		await older;
		assert.equal(state.device_access, access, 'the stale failure is not published over the newer listing');
	});
});

// [if] no deck has built the live graph yet and a device change's MAIN re-pin
// is still pending, a MAIN Test must refuse rather than beep a short-lived
// context on wherever the room was.
test("a MAIN Test with no live graph refuses while a device change's re-pin is pending (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		let landMain;
		const mainCalls = [];
		ctx.setSinkId = async (id) => {
			mainCalls.push(id);
			await new Promise((resolve) => (landMain = resolve));
		};
		// A MAIN restored from saved settings, with no deck yet: nothing built the graph.
		state.selected_master_output_device_id = DOCK;
		listOutputs([ROOM, PHONES]);
		const refresh = hp.refreshHeadphoneOutputs(source);
		await waitFor(() => landMain !== undefined, 'the re-pinned MAIN to reach setSinkId');
		assert.equal(hp.liveCalibrationGraph().nodes, null, 'precondition: no deck built the live graph');
		const opened = [];
		const saved = globalThis.AudioContext;
		globalThis.AudioContext = function () {
			opened.push('ctx');
			throw new Error('a short-lived Test context was opened');
		};
		try {
			for (const bus of ['master', 'input']) {
				await assert.rejects(hp.playIoTestBeeps(bus, state.selected_master_output_device_id), /MAIN is still switching/);
			}
			assert.deepEqual(opened, [], 'no short-lived context opened');
		} finally {
			globalThis.AudioContext = saved;
		}
		landMain();
		await refresh;
		globalThis.AudioContext = function () {
			opened.push('ctx');
			throw new Error('control: a short-lived Test context was opened');
		};
		try {
			await assert.rejects(hp.playIoTestBeeps('master', state.selected_master_output_device_id), /control: a short-lived Test context was opened/);
		} finally {
			globalThis.AudioContext = saved;
		}
	});
});

/** A shell output as `list` reports it. */
function shellDevice(uid, name, isDefault) {
	return { uid, name, channels: 2, transport: 'usb', is_default: isDefault, muted_by_jack: false, physical_uid: uid };
}

// [if] the dropped listing still wrote the room it saw, the next CUE pick would
// auto-pin MAIN to a room the newer listing says is no longer the default.
test("an older shell listing's room is dropped with it, so a CUE pick pins MAIN to the newer room (native)", async () => {
	const state = hp.mixerState.headphones;
	const BUS = 'native:SecondUsbInterface';
	const listing = (roomUid) =>
		[
			shellDevice('BuiltInSpeakerDevice', 'MacBook Pro Speakers', roomUid === 'BuiltInSpeakerDevice'),
			shellDevice('UsbInterface', 'USB Interface', false),
			shellDevice('SecondUsbInterface', 'Second USB Interface', roomUid === 'SecondUsbInterface')
		];
	nativeDevices = listing('BuiltInSpeakerDevice');
	holdTypes = ['list'];
	const older = hp.refreshHeadphoneOutputs(source);
	await waitFor(() => held.length === 1, 'the older listing to reach the shell');
	nativeDevices = listing('SecondUsbInterface');
	await hp.refreshHeadphoneOutputs(source);
	assert.ok(state.outputs.some((output) => output.id === BUS), 'precondition: the newer listing published');
	assert.equal(state.selected_master_output_device_id, BUS, 'precondition: the newer listing pinned MAIN to its room');
	held[0].ok();
	await older;
	assert.equal(state.selected_master_output_device_id, BUS, 'the older listing moved nothing');
	// The room is remembered past the listing: with no MAIN left, a CUE pick pins
	// MAIN to it, so the remembered one must be the newer listing's.
	state.selected_master_output_device_id = null;
	await hp.selectHeadphoneOutput(USB, source);
	assert.equal(state.selected_master_output_device_id, BUS, 'MAIN follows the newer listing\'s room');
	assert.equal(setMasterUids.at(-1), 'SecondUsbInterface');
});

// [if] the fix also dropped the room of the newest listing, MAIN would fall
// back to a label guess instead of staying where the room already plays.
test("control: the newest shell listing's room is the one a CUE pick pins MAIN to (native)", async () => {
	const state = hp.mixerState.headphones;
	nativeDevices = [
		shellDevice('BuiltInSpeakerDevice', 'MacBook Pro Speakers', false),
		shellDevice('UsbInterface', 'USB Interface', false),
		shellDevice('SecondUsbInterface', 'Second USB Interface', true)
	];
	await hp.refreshHeadphoneOutputs(source);
	state.selected_master_output_device_id = null;
	await hp.selectHeadphoneOutput(USB, source);
	assert.equal(state.selected_master_output_device_id, 'native:SecondUsbInterface');
});

// [if] a timed-out re-pin were forgotten, its move landing after a newer pick
// would leave the room on a device the menu no longer shows, with no switch
// pending to stop a Test beeping it.
test("a device-change re-pin that lands after its timeout is moved back to the newer MAIN pick (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		let finishRepin;
		ctx.sinkId = '';
		ctx.setSinkId = (id) => {
			if (ctx.holdNext) {
				ctx.holdNext = false;
				return new Promise((resolve) => (finishRepin = () => { ctx.sinkId = id; resolve(); }));
			}
			ctx.sinkId = id;
			return Promise.resolve();
		};
		await hp.selectMasterOutput(DOCK, source);
		ctx.holdNext = true;
		listOutputs([ROOM, PHONES, AUX]);
		mock.timers.enable({ apis: ['setTimeout'] });
		let refresh;
		try {
			refresh = hp.refreshHeadphoneOutputs(source).catch((error) => error);
			for (let i = 0; i < 50 && finishRepin === undefined; i++) await flushMicrotasks();
			assert.notEqual(finishRepin, undefined, 'the re-pin reached setSinkId');
			mock.timers.tick(5_000);
			await flushMicrotasks();
		} finally {
			mock.timers.reset();
		}
		await refresh;
		assert.equal(hp.outputSwitchesPending().master, true, 'the timed-out move still counts as MAIN switching');
		await hp.selectMasterOutput(AUX, source);
		assert.equal(ctx.sinkId, AUX, 'precondition: the newer pick landed first');
		finishRepin();
		await waitFor(() => ctx.sinkId === AUX && !hp.outputSwitchesPending().master, 'the correction to settle');
		assert.equal(state.selected_master_output_device_id, AUX);
		assert.equal(ctx.sinkId, AUX, 'the late re-pin is moved back to the MAIN shown');
	});
});

// [if] the fix also moved a late move that landed on the MAIN shown, MAIN
// would take a needless second move.
test("a timed-out re-pin that lands on the MAIN still shown moves nothing more, and reopens the room (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		let finishRepin;
		const calls = [];
		ctx.setSinkId = (id) => {
			calls.push(id);
			if (ctx.holdNext) {
				ctx.holdNext = false;
				return new Promise((resolve) => (finishRepin = resolve));
			}
			return Promise.resolve();
		};
		await hp.selectMasterOutput(DOCK, source);
		ctx.holdNext = true;
		listOutputs([ROOM, PHONES, AUX]);
		mock.timers.enable({ apis: ['setTimeout'] });
		let refresh;
		try {
			refresh = hp.refreshHeadphoneOutputs(source).catch((error) => error);
			for (let i = 0; i < 50 && finishRepin === undefined; i++) await flushMicrotasks();
			mock.timers.tick(5_000);
			await flushMicrotasks();
		} finally {
			mock.timers.reset();
		}
		await refresh;
		const sent = calls.length;
		const shown = state.selected_master_output_device_id;
		const suspendsBefore = ctx.suspends;
		finishRepin();
		await waitFor(() => !hp.outputSwitchesPending().master, 'the late move to settle');
		await new Promise((r) => setTimeout(r, 20));
		assert.equal(state.selected_master_output_device_id, shown);
		assert.equal(calls.length, sent, 'no correction move was sent');
		// Codex P1 BLOCKING on #6835: the late move is still a repeated move, so
		// the room is reopened once, not left `running` on a dead stream.
		assert.equal(ctx.suspends, suspendsBefore + 1, 'the room is reopened after the late landing');
	});
});

// [if] a MAIN move that never settles held MAIN switching forever, every Test
// would refuse and every later correction would wait on it.
// [if] a timed-out MAIN move lands only after its grace, once a Test may have
// started [then] the landing is announced as an output move, so that Test is cut
test('a timed-out MAIN move that lands after its grace is announced as an output move (browser path, Codex P2 BLOCKING on #6835)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		let finishRepin;
		ctx.setSinkId = (id) =>
			ctx.holdNext ? ((ctx.holdNext = false), new Promise((resolve) => (finishRepin = resolve))) : Promise.resolve(id);
		await hp.selectMasterOutput(DOCK, source);
		ctx.holdNext = true;
		listOutputs([ROOM, PHONES, AUX]);
		mock.timers.enable({ apis: ['setTimeout'] });
		try {
			const refresh = hp.refreshHeadphoneOutputs(source).catch((error) => error);
			for (let i = 0; i < 50 && finishRepin === undefined; i++) await flushMicrotasks();
			mock.timers.tick(5_000);
			await flushMicrotasks();
			await refresh;
			mock.timers.tick(10_000);
			await flushMicrotasks();
			assert.equal(hp.outputSwitchesPending().master, false, 'precondition: released after the grace');
		} finally {
			mock.timers.reset();
		}
		let moves = 0;
		const stop = hp.onOutputMoveStarted(() => (moves += 1));
		try {
			finishRepin();
			await waitFor(() => moves > 0, 'the late landing to be announced');
		} finally {
			stop();
		}
		await waitFor(() => !hp.outputSwitchesPending().master, 'the reopen to settle');
	});
});

test('a timed-out MAIN move that never settles stops counting as switching after the grace (browser path)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = (id) => (ctx.holdNext ? ((ctx.holdNext = false), new Promise(() => {})) : Promise.resolve(id));
		await hp.selectMasterOutput(DOCK, source);
		ctx.holdNext = true;
		listOutputs([ROOM, PHONES, AUX]);
		mock.timers.enable({ apis: ['setTimeout'] });
		try {
			const refresh = hp.refreshHeadphoneOutputs(source).catch((error) => error);
			await flushMicrotasks();
			mock.timers.tick(5_000);
			await flushMicrotasks();
			await refresh;
			assert.equal(hp.outputSwitchesPending().master, true, 'precondition: held past the operation timeout');
			mock.timers.tick(10_000);
			await flushMicrotasks();
			assert.equal(hp.outputSwitchesPending().master, false, 'released after the grace');
		} finally {
			mock.timers.reset();
		}
	});
});

/** A short-lived Test context whose pattern ends only when `endTone()` is
 * called or the Test is cut (a second stop). */
function heldToneContext(state) {
	const param = () => ({ value: 0, cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime() {}, setTargetAtTime() {} });
	return class {
		state = 'running';
		currentTime = 0;
		destination = {};
		createOscillator() {
			let stops = 0;
			const osc = {
				frequency: param(),
				connect() {},
				disconnect() {},
				start() {},
				stop() {
					stops += 1;
					if (stops === 1) state.endTone = () => osc.onended?.();
					else queueMicrotask(() => osc.onended?.());
				},
				onended: null
			};
			return osc;
		}
		createGain() {
			return { gain: param(), connect() {}, disconnect() {} };
		}
		async close() {
			this.state = 'closed';
		}
	};
}

// [if] a device change's re-pin ran under a sounding Test, the pattern would
// start on one device and finish on another, yet answer as played.
test("a device change's MAIN re-pin cuts a Test that is sounding (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		let landMove;
		ctx.setSinkId = (id) => new Promise((resolve) => (landMove = () => { ctx.sinkId = id; resolve(); }));
		state.selected_master_output_device_id = DOCK;
		const tone = {};
		globalThis.AudioContext = heldToneContext(tone);
		let result = 'still playing';
		const played = hp.playIoTestBeeps('master', DOCK).then((value) => (result = value));
		await waitFor(() => tone.endTone !== undefined, 'the Test to schedule its tones');
		listOutputs([ROOM, PHONES]);
		const refresh = hp.refreshHeadphoneOutputs(source);
		await waitFor(() => landMove !== undefined, 'the re-pin to start its move');
		// Cut as the move starts, not by the Test's own end-of-pattern timer.
		await waitFor(() => result !== 'still playing', 'the Test to be cut');
		assert.equal(result, false, 'the Test is cut, not answered as played');
		landMove();
		await refresh;
		await played;
	});
});

// [if] the picked MAIN vanishes mid-Test and the device change finds no replacement (MAIN cleared, no re-pin)
// [then] the Test is still cut and answers not played
test('a device change that clears MAIN cuts a Test that is sounding (browser path, Codex P2 BLOCKING on #6835)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async (id) => {
			ctx.sinkId = id;
		};
		state.selected_master_output_device_id = DOCK;
		const tone = {};
		globalThis.AudioContext = heldToneContext(tone);
		let result = 'still playing';
		const played = hp.playIoTestBeeps('master', DOCK).then((value) => (result = value));
		await waitFor(() => tone.endTone !== undefined, 'the Test to schedule its tones');
		listOutputs([AUX]);
		await hp.refreshHeadphoneOutputs(source);
		assert.equal(state.selected_master_output_device_id, null, 'precondition: MAIN cleared');
		await waitFor(() => result !== 'still playing', 'the Test to be cut');
		assert.equal(result, false, 'the Test on the vanished MAIN is cut, not answered as played');
		await played;
	});
});

// [if] the cut fired on every device change, a microphone plugged in mid-Test
// would cut a Test whose output never moved.
test('control: a device change that moves no output leaves a sounding Test playing (browser path)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		const moves = [];
		ctx.setSinkId = async (id) => {
			moves.push(id);
		};
		state.selected_master_output_device_id = DOCK;
		const tone = {};
		globalThis.AudioContext = heldToneContext(tone);
		const played = hp.playIoTestBeeps('master', DOCK);
		await waitFor(() => tone.endTone !== undefined, 'the Test to schedule its tones');
		listOutputs([ROOM, PHONES, BUS_OUT, DOCK, AUX]);
		await hp.refreshHeadphoneOutputs(source);
		assert.deepEqual(moves, [], 'precondition: nothing moved');
		tone.endTone();
		assert.equal(await played, true, 'the Test plays out');
	});
});

// [if] a remount forgot where MAIN landed, a short-lived Test context would
// follow the OS default, which may not be the MAIN the menu still shows.
test('a native MAIN Test with no live graph and no known landing refuses (native)', async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(SPEAKERS, source);
	hp.disposeHeadphoneMonitor();
	assert.equal(state.selected_master_output_device_id, SPEAKERS, 'precondition: the menu keeps its pick');
	assert.equal(hp.liveCalibrationGraph().nodes, null, 'precondition: the remount released the graph');
	const opened = [];
	const saved = globalThis.AudioContext;
	globalThis.AudioContext = function () {
		opened.push('ctx');
		throw new Error('a short-lived Test context was opened');
	};
	try {
		for (const bus of ['master', 'input']) {
			await assert.rejects(hp.playIoTestBeeps(bus, SPEAKERS), /MAIN has not landed on the picked device/);
		}
		assert.deepEqual(opened, []);
	} finally {
		globalThis.AudioContext = saved;
	}
});

// [if] the fix refused every native MAIN Test with no live graph, one whose
// MAIN a device change re-pinned (landed, no deck yet) could never be tested.
test('control: a native MAIN Test with no live graph plays once MAIN has landed (native)', async () => {
	const state = hp.mixerState.headphones;
	hp.disposeHeadphoneMonitor();
	state.selected_master_output_device_id = 'native:Vanished';
	nativeDevices = [
		{ uid: 'BuiltInSpeakerDevice', name: 'MacBook Pro Speakers', channels: 2, transport: 'builtin', is_default: false, muted_by_jack: false, physical_uid: 'BuiltInSpeakerDevice' },
		{ uid: 'UsbInterface', name: 'USB Interface', channels: 2, transport: 'usb', is_default: true, muted_by_jack: false, physical_uid: 'UsbInterface' }
	];
	await hp.refreshHeadphoneOutputs(source);
	assert.equal(hp.liveCalibrationGraph().nodes, null, 'precondition: no deck built the live graph');
	const main = state.selected_master_output_device_id;
	assert.equal(hp.landedMasterSinkId(), main, 'precondition: the re-pin landed MAIN');
	const opened = [];
	const saved = globalThis.AudioContext;
	globalThis.AudioContext = function () {
		opened.push('ctx');
		throw new Error('control: a short-lived Test context was opened');
	};
	try {
		await assert.rejects(hp.playIoTestBeeps('master', main), /control: a short-lived Test context was opened/);
		assert.deepEqual(opened, ['ctx']);
	} finally {
		globalThis.AudioContext = saved;
	}
});

// [if] the late move were counted across a teardown, the replacement mount
// would read MAIN as switching and refuse its Tests for the whole grace.
test("a retired mount's timed-out MAIN move does not read as the replacement's switch (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		let finishRepin;
		let hold;
		ctx.setSinkId = (id) => {
			if (ctx.holdNext) {
				ctx.holdNext = false;
				return new Promise((resolve) => (hold = resolve));
			}
			return Promise.resolve(id);
		};
		await hp.selectMasterOutput(DOCK, source);
		ctx.holdNext = true;
		listOutputs([ROOM, PHONES, AUX]);
		mock.timers.enable({ apis: ['setTimeout'] });
		try {
			const refresh = hp.refreshHeadphoneOutputs(source).catch((error) => error);
			for (let i = 0; i < 50 && hold === undefined; i++) await flushMicrotasks();
			finishRepin = hold;
			mock.timers.tick(5_000);
			await flushMicrotasks();
			await refresh;
		} finally {
			mock.timers.reset();
		}
		assert.equal(hp.outputSwitchesPending().master, true, 'precondition: the timed-out move counts for its own mount');
		hp.disposeHeadphoneMonitor();
		assert.equal(hp.outputSwitchesPending().master, false, 'the replacement mount reads no MAIN switch');
		finishRepin();
		await flushMicrotasks();
		assert.equal(hp.outputSwitchesPending().master, false);
		// The count stays balanced: the replacement's own pending move still reads as switching.
		hold = undefined;
		ctx.holdNext = true;
		browserOutputs(state);
		const pick = hp.selectMasterOutput(AUX, source);
		await waitFor(() => hold !== undefined, "the replacement's pick to reach setSinkId");
		assert.equal(hp.outputSwitchesPending().master, true, "the replacement's own move reads as switching");
		hold();
		await pick;
	});
});

// [if] the landing outlived the shell's connection, a MAIN Test would trust a
// pin the shell already dropped and beep whatever macOS defaults to next.
test('a native disconnect forgets where MAIN landed, so a MAIN Test refuses (native)', async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(SPEAKERS, source);
	assert.equal(hp.landedMasterSinkId(), SPEAKERS, 'precondition: MAIN landed');
	ReplyWorker.last.onmessage({ data: { kind: 'closed', reason: 'shell quit' } });
	assert.equal(hp.landedMasterSinkId(), null);
	assert.equal(state.selected_master_output_device_id, SPEAKERS, 'the menu keeps its pick');
	await assert.rejects(hp.playIoTestBeeps('master', SPEAKERS), /MAIN has not landed on the picked device/);
});

// [if] the clear reused the restore's CUE serial, the restore landing after it
// would switch the cleared CUE back on and watch an output the menu says is gone.
test('a CUE restore that lands after a newer device change cleared it again publishes nothing (browser path)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async () => {};
		await hp.selectMasterOutput(ROOM, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		listOutputs([ROOM, DOCK]);
		await hp.refreshHeadphoneOutputs(source);
		assert.equal(state.selected_output_device_id, null, 'precondition: the unplugged CUE is cleared');
		cue.hold(PHONES);
		listOutputs([ROOM, PHONES, DOCK]);
		const restore = hp.refreshHeadphoneOutputs(source);
		await waitFor(() => cue.isHeld(PHONES), 'the restored CUE to reach setSinkId');
		listOutputs([ROOM, DOCK]);
		await hp.refreshHeadphoneOutputs(source).catch((error) => error);
		assert.equal(state.selected_output_device_id, null, 'precondition: the newer change cleared the CUE again');
		assert.equal(state.active, false);
		cue.release(PHONES, true);
		await restore.catch((error) => error);
		await waitFor(() => !hp.outputSwitchesPending().cue, 'the CUE work to settle');
		assert.equal(state.selected_output_device_id, null, 'the late restore does not bring the CUE back');
		assert.equal(state.active, false, 'the late restore does not switch the cleared CUE on');
	});
});

// [if] the ordinary MAIN count survived a teardown, the replacement mount
// would refuse its MAIN Tests until the retired move settled or timed out.
test("a retired mount's MAIN move still inside its timeout does not read as the replacement's switch (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		let hold;
		ctx.setSinkId = (id) => {
			if (ctx.holdNext) {
				ctx.holdNext = false;
				return new Promise((resolve) => (hold = resolve));
			}
			return Promise.resolve(id);
		};
		await hp.selectMasterOutput(DOCK, source);
		ctx.holdNext = true;
		const old = hp.selectMasterOutput(AUX, source).catch((error) => error);
		await waitFor(() => hold !== undefined, 'the pick to reach setSinkId');
		assert.equal(hp.outputSwitchesPending().master, true, 'precondition: the pick counts for its own mount');
		const finishOld = hold;
		hp.disposeHeadphoneMonitor();
		assert.equal(hp.outputSwitchesPending().master, false, 'the replacement mount reads no MAIN switch');
		finishOld();
		await old;
		assert.equal(hp.outputSwitchesPending().master, false);
		// The count stays balanced: the replacement's own pending move still reads as switching.
		hold = undefined;
		ctx.holdNext = true;
		const pick = hp.selectMasterOutput(ROOM, source);
		await waitFor(() => hold !== undefined, "the replacement's pick to reach setSinkId");
		assert.equal(hp.outputSwitchesPending().master, true, "the replacement's own move reads as switching");
		hold();
		await pick;
	});
});

// [if] an older device change re-pins MAIN and also plans to clear the vanished
// CUE, and a newer change restores that CUE while the re-pin is still pending,
// the older change must not run its stale clear afterwards.
// [then] the restored CUE keeps sounding.
test("an older device change's CUE clear does not run after a newer change restored the CUE (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		let hold;
		ctx.setSinkId = (id) => {
			if (ctx.holdNext) {
				ctx.holdNext = false;
				return new Promise((resolve) => (hold = () => { ctx.sinkId = id; resolve(); }));
			}
			ctx.sinkId = id;
			return Promise.resolve();
		};
		await hp.selectMasterOutput(DOCK, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		ctx.holdNext = true;
		listOutputs([ROOM]);
		const older = hp.refreshHeadphoneOutputs(source).catch((error) => error);
		await waitFor(() => hold !== undefined, "the older change's MAIN re-pin to reach setSinkId");
		listOutputs([ROOM, PHONES]);
		await hp.refreshHeadphoneOutputs(source);
		assert.equal(state.selected_output_device_id, PHONES, 'precondition: the newer change restored the CUE');
		assert.equal(state.active, true, 'precondition: the restored CUE is on');
		hold();
		await older;
		await waitFor(() => !hp.outputSwitchesPending().cue && !hp.outputSwitchesPending().master, 'the output work to settle');
		assert.equal(state.selected_output_device_id, PHONES);
		assert.equal(state.active, true, 'the stale clear did not switch the restored CUE off');
		assert.equal(cue.state, 'running', 'nor silence it');
	});
});

// [if] one device change re-pins MAIN and clears the vanished CUE, with no
// newer change behind it, its CUE clear still runs (the opposite direction).
// [then] the CUE is switched off and silenced.
test("control: a lone device change that re-pins MAIN still clears the vanished CUE (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		ctx.setSinkId = async (id) => {
			ctx.sinkId = id;
		};
		await hp.selectMasterOutput(DOCK, source);
		await hp.selectHeadphoneOutput(PHONES, source);
		assert.equal(state.active, true, 'precondition: the CUE is on');
		listOutputs([ROOM]);
		await hp.refreshHeadphoneOutputs(source).catch((error) => error);
		await waitFor(() => !hp.outputSwitchesPending().cue && !hp.outputSwitchesPending().master, 'the output work to settle');
		assert.equal(ctx.sinkId, ROOM, 'precondition: MAIN was re-pinned');
		assert.equal(state.active, false, 'the vanished CUE is switched off');
		assert.notEqual(cue.state, 'running', 'and silenced');
	});
});

// [if] a device change enters a same-device split and a newer device change
// lists the outputs apart while the split's MAIN move is pending [then] the old
// split publishes nothing over the newer listing, and MAIN is moved back.
test("an older device change's split does not publish over a newer listing that keeps two outputs", async () => {
	const state = hp.mixerState.headphones;
	const device = (uid, name, physical) => ({ uid, name, channels: 2, transport: 'usb', is_default: false, muted_by_jack: false, physical_uid: physical });
	await hp.selectMasterOutput(USB, source);
	await hp.selectHeadphoneOutput(SPEAKERS, source);
	assert.equal(state.output_mode, 'two_outputs', 'precondition: two outputs');
	// The shell first reports both on one physical output: a split.
	nativeDevices = [device('BuiltInSpeakerDevice', 'Speakers', 'BuiltInSpeakerDevice'), device('UsbInterface', 'USB', 'BuiltInSpeakerDevice')];
	holdTypes = ['set_master'];
	const older = hp.refreshHeadphoneOutputs(source).catch((error) => error);
	await waitFor(() => held.length === 1, "the split's MAIN move to be sent");
	// Then it reports them apart again before that move lands.
	nativeDevices = [device('BuiltInSpeakerDevice', 'Speakers', 'BuiltInSpeakerDevice'), device('UsbInterface', 'USB', 'UsbInterface')];
	await hp.refreshHeadphoneOutputs(source);
	held[0].ok();
	await older;
	await waitFor(() => !hp.outputSwitchesPending().master, 'MAIN to settle');
	assert.equal(state.output_mode, 'two_outputs', 'the older split must not publish split_cable');
	assert.equal(state.split_reason, null);
	assert.equal(state.selected_output_device_id, SPEAKERS, 'the CUE the newer listing kept stands');
	assert.equal(state.selected_master_output_device_id, USB);
	assert.equal(setMasterUids.at(-1), 'UsbInterface', 'MAIN is moved back to the MAIN shown');
});

test("control: a lone device change that lists MAIN and CUE on one output still publishes the split", async () => {
	const state = hp.mixerState.headphones;
	const device = (uid, name, physical) => ({ uid, name, channels: 2, transport: 'usb', is_default: false, muted_by_jack: false, physical_uid: physical });
	await hp.selectMasterOutput(USB, source);
	await hp.selectHeadphoneOutput(SPEAKERS, source);
	nativeDevices = [device('BuiltInSpeakerDevice', 'Speakers', 'BuiltInSpeakerDevice'), device('UsbInterface', 'USB', 'BuiltInSpeakerDevice')];
	holdTypes = ['set_master'];
	const refresh = hp.refreshHeadphoneOutputs(source).catch((error) => error);
	await waitFor(() => held.length === 1, "the split's MAIN move to be sent");
	held[0].ok();
	assert.equal(await refresh, undefined);
	assert.equal(state.output_mode, 'split_cable');
	assert.equal(state.split_reason, 'same_device');
});

/** Pick `deviceId` for CUE with its native open held past the timeout. */
async function timedOutNativeOpen(deviceId) {
	holdTypes = ['open'];
	mock.timers.enable({ apis: ['setTimeout'] });
	let pick;
	try {
		pick = hp.selectHeadphoneOutput(deviceId, source).catch((error) => error);
		for (let i = 0; i < 50 && held.length === 0; i++) await flushMicrotasks();
		assert.equal(held.length, 1, 'the pick sent its native open');
		mock.timers.tick(5_000);
		await flushMicrotasks();
	} finally {
		mock.timers.reset();
	}
	const error = await pick;
	assert.match(String(error), /timed out/, 'precondition: the pick timed out');
}

test('a native CUE open that lands after its timeout and a newer pick is reopened on the CUE shown', async () => {
	const state = hp.mixerState.headphones;
	const BUS = 'native:SecondUsbInterface';
	const THIRD = 'native:ThirdUsbInterface';
	state.outputs = [...state.outputs, { id: BUS, label: 'Second USB Interface' }, { id: THIRD, label: 'Third USB Interface' }];
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	await timedOutNativeOpen(BUS);
	await hp.selectHeadphoneOutput(THIRD, source);
	assert.equal(nativeCueOpen, 'ThirdUsbInterface', 'precondition: the newer pick opened');
	held[0].ok();
	await waitFor(() => !hp.outputSwitchesPending().cue, 'the correction to settle');
	assert.equal(state.selected_output_device_id, THIRD);
	assert.equal(nativeCueOpen, 'ThirdUsbInterface', 'the shell CUE is back on the CUE shown');
});

test('a native CUE open that lands after its own pick timed out and rolled back is reopened on the CUE shown', async () => {
	const state = hp.mixerState.headphones;
	const BUS = 'native:SecondUsbInterface';
	state.outputs = [...state.outputs, { id: BUS, label: 'Second USB Interface' }];
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	await timedOutNativeOpen(BUS);
	assert.equal(state.selected_output_device_id, USB, 'precondition: the menu rolled back');
	held[0].ok();
	await waitFor(() => !hp.outputSwitchesPending().cue, 'the correction to settle');
	assert.equal(state.selected_output_device_id, USB);
	assert.equal(nativeCueOpen, 'UsbInterface', 'the shell CUE is back on the CUE shown');
});

test('control: a native CUE open that lands in time with no newer pick sends nothing more', async () => {
	const state = hp.mixerState.headphones;
	await hp.selectMasterOutput(SPEAKERS, source);
	holdTypes = ['open'];
	const pick = hp.selectHeadphoneOutput(USB, source);
	await waitFor(() => held.length === 1, 'the pick to open its device');
	const sent = cueCommands.length;
	held[0].ok();
	await pick;
	await waitFor(() => !hp.outputSwitchesPending().cue, 'the CUE work to settle');
	await new Promise((r) => setTimeout(r, 20));
	assert.equal(state.selected_output_device_id, USB);
	assert.equal(nativeCueOpen, 'UsbInterface');
	assert.equal(cueCommands.length, sent, 'no correction follows an open that landed in time');
});

/** A browser room whose next setSinkId is held until `release(ok)`. */
function holdNextRoomSinkMove() {
	const hold = { release: undefined };
	ctx.sinkId = '';
	ctx.setSinkId = (id) => {
		if (ctx.holdNext) {
			ctx.holdNext = false;
			return new Promise((resolve, reject) => {
				hold.release = (ok) => (ok ? ((ctx.sinkId = id), resolve()) : reject(new Error('NotFoundError: the old room went away')));
			});
		}
		ctx.sinkId = id;
		return Promise.resolve();
	};
	return hold;
}

for (const lands of [true, false]) {
	test(`a device change's MAIN re-pin that ${lands ? 'lands' : 'fails'} after a newer one cleared MAIN publishes nothing (browser path)`, async () => {
		const state = hp.mixerState.headphones;
		const cue = browserCueContext();
		await onBrowserPath(() => cue, async () => {
			browserOutputs(state);
			const hold = holdNextRoomSinkMove();
			await hp.selectMasterOutput(DOCK, source);
			ctx.holdNext = true;
			listOutputs([ROOM, PHONES, AUX]);
			const older = hp.refreshHeadphoneOutputs(source).catch((error) => error);
			await waitFor(() => hold.release !== undefined, 'the re-pin to reach setSinkId');
			// The newer listing has no room output to re-pin to: MAIN goes to the system default.
			listOutputs([AUX]);
			await hp.refreshHeadphoneOutputs(source);
			assert.equal(state.selected_master_output_device_id, null, 'precondition: MAIN cleared');
			const suspendsBefore = ctx.suspends;
			hold.release(lands);
			assert.equal(await older, undefined, 'the older device change does not fail over the newer one');
			await waitFor(() => !hp.outputSwitchesPending().master, 'MAIN to settle');
			assert.equal(state.selected_master_output_device_id, null);
			assert.notEqual(state.routes.master.state, 'selected', 'the stale re-pin publishes no MAIN route');
			assert.equal(state.error, null, 'nor shows its failure');
			if (lands) assert.equal(ctx.sinkId, '', 'the sink goes back to the system default');
			// That correction is a repeated move: the room is reopened after it (Codex P1 BLOCKING on #6835).
			if (lands) await waitFor(() => ctx.suspends > suspendsBefore, 'the room to be reopened after the move back');
			// Control: a move back to the default that lands leaves MAIN usable.
			if (lands) assert.notEqual(state.routes.master.state, 'failed');
		});
	});
}

for (const lands of [true, false]) {
	// [if] a device change replaces MAIN while an operator MAIN pick's move is pending
	// [then] the pick is retired: landing, it is moved back to the MAIN shown; failing, it fails nothing
	test(`a MAIN pick whose move ${lands ? 'lands' : 'fails'} after a device change replaced MAIN is retired (browser path, Codex P1 BLOCKING on #6835)`, async () => {
		const state = hp.mixerState.headphones;
		const cue = browserCueContext();
		await onBrowserPath(() => cue, async () => {
			browserOutputs(state);
			listOutputs([ROOM, PHONES, BUS_OUT, DOCK, AUX]);
			const hold = holdNextRoomSinkMove();
			await hp.selectMasterOutput(AUX, source);
			ctx.holdNext = true;
			const pick = hp.selectMasterOutput(DOCK, source).catch((error) => error);
			await waitFor(() => hold.release !== undefined, 'the pick to reach setSinkId');
			// DOCK is unplugged: the device change moves MAIN to the room output.
			listOutputs([ROOM, PHONES, AUX]);
			await hp.refreshHeadphoneOutputs(source);
			assert.equal(state.selected_master_output_device_id, ROOM, 'precondition: the device change replaced MAIN');
			hold.release(lands);
			assert.equal(await pick, undefined, 'the retired pick does not reject over the device change');
			await waitFor(() => !hp.outputSwitchesPending().master, 'MAIN to settle');
			assert.equal(state.selected_master_output_device_id, ROOM, 'the vanished MAIN is not republished');
			assert.notEqual(state.routes.master.state, 'failed', 'nor is the refreshed route failed');
			assert.equal(ctx.sinkId, ROOM, 'the sink ends on the MAIN shown');
		});
	});
}

// [if] a device change clears MAIN while a MAIN pick's move is pending and that move lands
// [then] the sink is moved back to the system default the menu shows
test('a MAIN pick that lands after a device change cleared MAIN is moved back to the system default (browser path, Codex P1 BLOCKING on #6835)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		listOutputs([ROOM, PHONES, BUS_OUT, DOCK, AUX]);
		const hold = holdNextRoomSinkMove();
		await hp.selectMasterOutput(AUX, source);
		ctx.holdNext = true;
		const pick = hp.selectMasterOutput(DOCK, source).catch((error) => error);
		await waitFor(() => hold.release !== undefined, 'the pick to reach setSinkId');
		listOutputs([AUX]);
		await hp.refreshHeadphoneOutputs(source);
		assert.equal(state.selected_master_output_device_id, null, 'precondition: the device change cleared MAIN');
		hold.release(true);
		assert.equal(await pick, undefined);
		await waitFor(() => !hp.outputSwitchesPending().master && ctx.sinkId === '', 'the sink back on the default');
		assert.equal(state.selected_master_output_device_id, null);
		assert.equal(state.routes.master.state, 'default');
	});
});

for (const lands of [true, false]) {
	// [if] a late explicit move lands under an explicitly picked 'default' row and the move back [lands | fails]
	// [then] MAIN stays selected on the default | MAIN is marked failed, so no Test answers for the default
	test(`a correction back to an explicitly picked default that ${lands ? 'lands keeps MAIN selected' : 'fails marks MAIN failed'} (browser path, Codex P2 BLOCKING on #6835)`, async () => {
		const state = hp.mixerState.headphones;
		const cue = browserCueContext();
		await onBrowserPath(() => cue, async () => {
			state.outputs = ['default', ROOM, DOCK, AUX].map((id) => ({ id, label: id }));
			listOutputs(['default', ROOM, DOCK, AUX]);
			let releaseDock;
			let failNextDefault = false;
			ctx.sinkId = '';
			ctx.setSinkId = (id) => {
				if (id === DOCK) return new Promise((resolve) => (releaseDock = () => ((ctx.sinkId = id), resolve())));
				if (id === '' && failNextDefault) {
					failNextDefault = false;
					return Promise.reject(new Error('NotAllowedError: default refused'));
				}
				ctx.sinkId = id;
				return Promise.resolve();
			};
			await hp.selectMasterOutput(AUX, source);
			const older = hp.selectMasterOutput(DOCK, source).catch((error) => error);
			await waitFor(() => releaseDock !== undefined, 'the DOCK move to reach setSinkId');
			await hp.selectMasterOutput('default', source);
			assert.equal(ctx.sinkId, '', 'precondition: the default pick landed');
			failNextDefault = !lands;
			releaseDock();
			await older;
			await waitFor(() => !hp.outputSwitchesPending().master, 'the correction to settle');
			assert.equal(state.selected_master_output_device_id, 'default');
			if (lands) {
				assert.equal(ctx.sinkId, '', 'the late DOCK move is put back on the default');
				assert.equal(state.routes.master.state, 'selected');
			} else {
				assert.equal(ctx.sinkId, DOCK, 'precondition: the sink is stranded on DOCK');
				assert.equal(state.routes.master.state, 'failed', 'MAIN is not reported on the default it left');
			}
		});
	});
}

// [if] in the Mac app a device change clears MAIN while a MAIN pick's set_master is pending, and it lands
// [then] no set_master to a "default" device is sent: the shell's default is its pin (control)
test('control: a MAIN pick that lands after a device change cleared MAIN sends no default move in the Mac app', async () => {
	const state = hp.mixerState.headphones;
	const device = (uid, name) => ({ uid, name, channels: 2, transport: 'usb', is_default: false, muted_by_jack: false, physical_uid: uid });
	await hp.selectMasterOutput(SPEAKERS, source);
	holdTypes = ['set_master'];
	const pick = hp.selectMasterOutput(USB, source).catch((error) => error);
	await waitFor(() => held.length === 1, 'the pick to send set_master');
	nativeDevices = [device('HdmiDisplay', 'HDMI')];
	await hp.refreshHeadphoneOutputs(source);
	assert.equal(state.selected_master_output_device_id, null, 'precondition: the device change cleared MAIN');
	const sent = setMasterUids.length;
	held[0].ok();
	await pick;
	await waitFor(() => !hp.outputSwitchesPending().master, 'MAIN to settle');
	await new Promise((r) => setTimeout(r, 20));
	assert.deepEqual(setMasterUids.slice(sent), [], 'no correction is sent to the shell');
	assert.notEqual(state.routes.master.state, 'failed');
});

for (const lands of [true, false]) {
	// [if] a device change clears MAIN while the live room is on that MAIN's sink and the move back to the default [lands | fails]
	// [then] the room is moved to the system default before a Test trusts it | MAIN is marked failed
	test(`a device change that clears MAIN moves the live room to the default, ${lands ? 'landing' : 'failing closed'} (browser path, Codex P2 BLOCKING on #6835)`, async () => {
		const state = hp.mixerState.headphones;
		const cue = browserCueContext();
		await onBrowserPath(() => cue, async () => {
			browserOutputs(state);
			listOutputs([ROOM, PHONES, BUS_OUT, DOCK, AUX]);
			let refuseDefault = false;
			ctx.sinkId = '';
			ctx.setSinkId = (id) => {
				if (id === '' && refuseDefault) return Promise.reject(new Error('NotAllowedError: default refused'));
				ctx.sinkId = id;
				return Promise.resolve();
			};
			await hp.selectMasterOutput(DOCK, source);
			assert.equal(ctx.sinkId, DOCK, 'precondition: the room is on DOCK');
			refuseDefault = !lands;
			listOutputs([AUX]);
			await hp.refreshHeadphoneOutputs(source);
			assert.equal(state.selected_master_output_device_id, null, 'precondition: MAIN cleared');
			await waitFor(() => !hp.outputSwitchesPending().master, 'the move back to settle');
			if (lands) {
				assert.equal(ctx.sinkId, '', 'the room is on the system default');
				assert.equal(state.routes.master.state, 'default');
			} else {
				assert.equal(ctx.sinkId, DOCK, 'precondition: the room is stranded on the vanished sink');
				assert.equal(state.routes.master.state, 'failed', 'no Test answers for a default the room never reached');
			}
		});
	});
}

// [if] MAIN was never picked (the system default) and a device change lists a new output under a sounding Test
// [then] no move back to the default is made and the Test plays out (control)
test('control: a device change with MAIN already on the default moves nothing and leaves a Test playing (browser path)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		const moves = [];
		ctx.setSinkId = async (id) => {
			moves.push(id);
		};
		hp.ensureHeadphoneGraph(ctx, ctx.createGain());
		assert.equal(state.selected_master_output_device_id, null, 'precondition: MAIN on the default');
		const tone = {};
		const restore = beepOnLiveGraph(tone);
		try {
			const played = hp.playIoTestBeeps('master', null);
			await waitFor(() => tone.endTone !== undefined, 'the Test to schedule its tones');
			// A device change that leaves no output to pin MAIN to (a microphone, say).
			listOutputs([AUX]);
			await hp.refreshHeadphoneOutputs(source);
			assert.equal(state.selected_master_output_device_id, null, 'precondition: MAIN stays on the default');
			await new Promise((r) => setTimeout(r, 10));
			assert.deepEqual(moves, [], 'nothing is moved');
			tone.endTone();
			assert.equal(await played, true, 'the Test plays out');
		} finally {
			restore();
		}
	});
});

// [if] the output a same-device split ran on vanishes and no output is left for MAIN
// [then] the room is moved to the system default too, not left on the vanished sink
test('a vanished same-device split moves the room to the system default (browser path, Codex P1 BLOCKING on #6835)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		const moves = [];
		ctx.setSinkId = async (id) => {
			moves.push(id);
			ctx.sinkId = id;
		};
		await hp.selectMasterOutput(DOCK, source);
		// The split on DOCK, as _enterSameDeviceSplit leaves it.
		state.output_mode = 'split_cable';
		state.split_reason = 'same_device';
		moves.length = 0;
		const suspendsBefore = ctx.suspends;
		listOutputs([AUX]);
		await hp.refreshHeadphoneOutputs(source);
		await waitFor(() => !hp.outputSwitchesPending().master, 'MAIN to settle');
		assert.equal(state.output_mode, 'two_outputs', 'precondition: the split ended');
		assert.equal(state.selected_master_output_device_id, null);
		assert.deepEqual(moves, [''], 'the room leaves the vanished sink for the system default');
		await waitFor(() => ctx.suspends === suspendsBefore + 1, 'the room to be reopened after the move');
		assert.equal(state.routes.master.state, 'default');
	});
});

// [if] a refresh that leaves MAIN alone lands after a failed room reopen
// [then] the retry error stays shown until a reopen succeeds
test("a refresh keeps a failed reopen's retry error until a reopen succeeds (browser path, Codex P2 BLOCKING on #6835)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		listOutputs([ROOM, PHONES, DOCK]);
		ctx.setSinkId = async (id) => {
			ctx.sinkId = id;
		};
		const resume = ctx.resume;
		ctx.resume = async () => {
			throw new Error('resume died');
		};
		try {
			await hp.selectMasterOutput(DOCK, source);
		} finally {
			ctx.resume = resume;
		}
		assert.match(state.error ?? '', /Pick the device again/, 'precondition: the reopen failed');
		listOutputs([ROOM, PHONES, DOCK, 'new-headset']);
		await hp.refreshHeadphoneOutputs(source);
		assert.match(state.error ?? '', /resume died.*Pick the device again/, 'the refresh keeps the recovery instruction');
		// Control: once a re-pick reopens the room, a refresh clears it again.
		await hp.selectMasterOutput(DOCK, source);
		assert.equal(state.error, null);
		state.error = 'an earlier listing failed';
		await hp.refreshHeadphoneOutputs(source);
		assert.equal(state.error, null, 'with no failed reopen a clean refresh clears an old error');
	});
});

// [if] a device change that keeps MAIN lands while a MAIN pick's move is pending [then] the pick still publishes its route (control)
test('control: a MAIN pick survives a device change that keeps MAIN (browser path)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		listOutputs([ROOM, PHONES, BUS_OUT, DOCK, AUX]);
		const hold = holdNextRoomSinkMove();
		await hp.selectMasterOutput(AUX, source);
		ctx.holdNext = true;
		const pick = hp.selectMasterOutput(DOCK, source);
		await waitFor(() => hold.release !== undefined, 'the pick to reach setSinkId');
		// An unrelated device appears; DOCK stays.
		listOutputs([ROOM, PHONES, BUS_OUT, DOCK, AUX, 'new-headset']);
		await hp.refreshHeadphoneOutputs(source);
		assert.equal(state.selected_master_output_device_id, DOCK);
		hold.release(true);
		await pick;
		assert.equal(state.routes.master.state, 'selected', 'the pick still publishes its MAIN route');
		assert.equal(savedMainId(), DOCK, 'and saves it');
		assert.equal(ctx.sinkId, DOCK);
	});
});

test("control: a lone device change's MAIN re-pin still publishes the MAIN route (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		const hold = holdNextRoomSinkMove();
		await hp.selectMasterOutput(DOCK, source);
		ctx.holdNext = true;
		listOutputs([ROOM, PHONES, AUX]);
		const refresh = hp.refreshHeadphoneOutputs(source).catch((error) => error);
		await waitFor(() => hold.release !== undefined, 'the re-pin to reach setSinkId');
		hold.release(true);
		assert.equal(await refresh, undefined);
		await waitFor(() => !hp.outputSwitchesPending().master, 'MAIN to settle');
		assert.equal(state.selected_master_output_device_id, ROOM);
		assert.equal(state.routes.master.state, 'selected');
		assert.equal(ctx.sinkId, ROOM);
	});
});

test("control: a lone device change's MAIN re-pin that fails still reports its failure (browser path)", async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		const hold = holdNextRoomSinkMove();
		await hp.selectMasterOutput(DOCK, source);
		ctx.holdNext = true;
		listOutputs([ROOM, PHONES, AUX]);
		const refresh = hp.refreshHeadphoneOutputs(source).catch((error) => error);
		await waitFor(() => hold.release !== undefined, 'the re-pin to reach setSinkId');
		hold.release(false);
		const failure = await refresh;
		assert.match(String(failure ?? state.error), /old room went away/, 'the current re-pin failure is not swallowed');
	});
});

// [if] the move back to the system default after a stale re-pin fails (the sink may stay on the re-pin's device)
// [then] MAIN is marked failed, so a MAIN Test refuses rather than answering for the default
test('a move back to the system default that fails marks MAIN failed (browser path, Codex P2 BLOCKING on #6835)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		const holds = [];
		ctx.sinkId = '';
		ctx.holdCount = 0;
		ctx.setSinkId = (id) => {
			if (ctx.holdCount > 0) {
				ctx.holdCount -= 1;
				return new Promise((resolve, reject) =>
					holds.push((ok) => (ok ? ((ctx.sinkId = id), resolve()) : reject(new Error('NotAllowedError: default refused'))))
				);
			}
			ctx.sinkId = id;
			return Promise.resolve();
		};
		await hp.selectMasterOutput(DOCK, source);
		ctx.holdCount = 2;
		listOutputs([ROOM, PHONES, AUX]);
		const older = hp.refreshHeadphoneOutputs(source).catch((error) => error);
		await waitFor(() => holds.length === 1, 'the re-pin to reach setSinkId');
		listOutputs([AUX]);
		await hp.refreshHeadphoneOutputs(source);
		holds[0](true);
		await older;
		await waitFor(() => holds.length === 2, 'the move back to the default to reach setSinkId');
		assert.equal(ctx.sinkId, ROOM, 'precondition: the sink sits on the stale re-pin device');
		holds[1](false);
		await waitFor(() => !hp.outputSwitchesPending().master, 'MAIN to settle');
		assert.equal(state.selected_master_output_device_id, null);
		assert.equal(state.routes.master.state, 'failed', 'MAIN is not reported on the default it never reached');
	});
});

test('a MAIN pick that lands while the move back to the system default is pending is moved back to (browser path)', async () => {
	const state = hp.mixerState.headphones;
	const cue = browserCueContext();
	await onBrowserPath(() => cue, async () => {
		browserOutputs(state);
		const holds = [];
		ctx.sinkId = '';
		ctx.holdCount = 0;
		ctx.setSinkId = (id) => {
			if (ctx.holdCount > 0) {
				ctx.holdCount -= 1;
				return new Promise((resolve) => holds.push(() => ((ctx.sinkId = id), resolve())));
			}
			ctx.sinkId = id;
			return Promise.resolve();
		};
		await hp.selectMasterOutput(DOCK, source);
		// Hold the re-pin, then the move back to the default it triggers.
		ctx.holdCount = 2;
		listOutputs([ROOM, PHONES, AUX]);
		const older = hp.refreshHeadphoneOutputs(source).catch((error) => error);
		await waitFor(() => holds.length === 1, 'the re-pin to reach setSinkId');
		listOutputs([AUX]);
		await hp.refreshHeadphoneOutputs(source);
		holds[0]();
		await older;
		await waitFor(() => holds.length === 2, 'the move back to the default to reach setSinkId');
		listOutputs([ROOM, PHONES, AUX]);
		await hp.selectMasterOutput(AUX, source);
		assert.equal(ctx.sinkId, AUX, 'precondition: the pick landed first');
		holds[1]();
		await waitFor(() => ctx.sinkId === AUX && !hp.outputSwitchesPending().master, 'the correction to settle');
		assert.equal(state.selected_master_output_device_id, AUX);
		assert.equal(ctx.sinkId, AUX, 'the late default move is moved back to the MAIN picked');
	});
});

/** The Mac app's Test beeps on the live room graph: give the room context a
 * held tone (see heldToneContext) for the length of one test. */
function beepOnLiveGraph(tone) {
	const Held = heldToneContext(tone);
	const held = new Held();
	const saved = { createOscillator: ctx.createOscillator, createGain: ctx.createGain };
	ctx.createOscillator = () => held.createOscillator();
	ctx.createGain = () => held.createGain();
	return () => Object.assign(ctx, saved);
}

for (const event of [
	{ type: 'device_lost', uid: 'UsbInterface' },
	{ type: 'master_pin_released', reason: 'cannot_sound', message: 'MAIN pin released' }
]) {
	test(`a sounding Test is cut when the shell reports ${event.type}`, async () => {
		const state = hp.mixerState.headphones;
		await hp.selectMasterOutput(SPEAKERS, source);
		await hp.selectHeadphoneOutput(USB, source);
		const tone = {};
		const restore = beepOnLiveGraph(tone);
		try {
			let result = 'still playing';
			const played = hp.playIoTestBeeps('master', SPEAKERS).then((value) => (result = value), (error) => (result = error));
			await waitFor(() => tone.endTone !== undefined || result !== 'still playing', 'the Test to schedule its tones');
			assert.equal(result, 'still playing', `precondition: the Test is sounding (${result})`);
			ReplyWorker.last.onmessage({ data: { kind: 'message', payload: event } });
			await waitFor(() => result !== 'still playing', 'the Test to be cut');
			assert.equal(result, false, 'the Test is cut, not answered as played');
			await played;
		} finally {
			restore();
			state.error = null;
		}
	});
}

test('control: a shell stats event leaves a sounding Test playing', async () => {
	await hp.selectMasterOutput(SPEAKERS, source);
	await hp.selectHeadphoneOutput(USB, source);
	const tone = {};
	const restore = beepOnLiveGraph(tone);
	try {
		const played = hp.playIoTestBeeps('master', SPEAKERS);
		await waitFor(() => tone.endTone !== undefined, 'the Test to schedule its tones');
		ReplyWorker.last.onmessage({ data: { kind: 'message', payload: { type: 'stats' } } });
		tone.endTone();
		assert.equal(await played, true, 'the Test plays out');
	} finally {
		restore();
	}
});

// The shell connection closing ends the sink client for this file's later
// tests, so that edge and the cue bridge failure are pinned in the source.
test('a shell disconnect or a failed cue bridge cuts a sounding Test', () => {
	const source = readFileSync(new URL('../../src/lib/player/headphones.ts', import.meta.url), 'utf8');
	const bridge = source.slice(source.indexOf('export async function failCueBridge('));
	assert.match(bridge.slice(0, 1500), /mixerState\.headphones\.active = false;\n\t_outputMoveStarted\(\);/);
	const closed = source.slice(source.indexOf("case 'disconnected':"));
	assert.match(closed.slice(0, 600), /_landedMasterSinkId = null;\n\t\t\t_outputMoveStarted\(\);/);
});
