// requirement: IOPIN-18 (master output switches never strand MAIN silent)
// [if] a MAIN device pick lands [then] the room context is suspended and resumed so the browser reopens its stream
// [if] the operator picks the MAIN device already shown [then] the stream is reopened and the live route is never marked failed
// [if] an older MAIN pick fails after a newer pick started [then] it neither rolls the menu back nor rejects
// [if] an older pick's reopen fails after a newer pick succeeded [then] the newer pick's cleared error stands, the room ends running and the stale failure is recorded
// [if] the room context cannot be reopened (no suspend/resume, or closed) [then] the pick shows a retry error, never a silent pass
// [if] a repeat pick follows a failed reopen that left the room suspended [then] it resumes the room and the retry error clears
// [if] the room context is already suspended at the pick, including a pause after an earlier clean reopen [then] it is left suspended, with no error
// [if] the monitor is disposed while a pick reopens [then] the pick publishes nothing
// [if] the device is picked again while its first sink move is still pending and that move fails [then] the repeat pick moves the sink itself and the stale failure is recorded
// [if] an older pick's sink move lands after a newer pick failed [then] the newer pick's failed route stands
// [if] a pick is refused by validation while another pick is in flight, during its sink move or its reopen [then] the in-flight pick still lands, reopens, is saved and shows route selected
// [if] an older pick's sink move lands after a newer pick's [then] the sink is moved back to the device the menu shows and the room is reopened
// [if] a newer pick lands while that corrective move is pending [then] the correction follows the newest pick
// [if] an older pick lands while another older move is still pending, and that move then fails [then] the deferred correction runs and moves the sink back to the device shown
// [if] the shown MAIN is picked again with no landing known since a remount [then] the sink is moved to it, never skipped as a repeat
// [if] the headphone routes are torn down while a corrective sink move is pending [then] the correction stops: no further move, no reopen, no error
// [if] a repeat pick's OS-default re-assert lands after a newer pick's move [then] the sink is moved back to the device the menu shows
// [if] a teardown retires a pick and a new mount publishes its own MAIN before the pick settles [then] the pick rejects as stale and neither rolls back nor fails that MAIN
// [if] a pick a teardown retired lands its sink move after the replacement mount picked [then] nothing is moved, reopened or reported through the retired room
// [if] newer picks keep landing while each corrective pass is pending [then] the correction keeps following until the sink matches the menu
// [if] the app resumes a room a failed reopen left suspended, then pauses it [then] the next pick leaves that pause alone
// [if] a reopen's suspend fails or times out with the room still running [then] a later pause by the app is left alone
// [if] a timed-out reopen suspend lands later [then] the next reopen resumes that room
// [if] a timed-out reopen suspend lands later while a deck plays [then] the watchdog raises no unexpected-pause alarm for it, and a pause after it settles still alarms
// [if] a timed-out reopen suspend never settles [then] its watchdog excuse ends after a bounded grace, so a later genuine pause still alarms
// [if] a timed-out reopen suspend lands after that grace [then] its edge is still not an unexpected pause, the next reopen resumes the room, and a later pause still alarms
// [if] something else pauses the room while that late suspend is pending, and the suspend settles a task or more later [then] that pause still alarms, is never resumed by a reopen as ours, and excuses no later edge
// [if] that late suspend's promise settles before its statechange edge is dispatched (the spec order) [then] the edge is still excused, and only that edge
// [if] two reopens overlap and the later one's suspend times out after the earlier one silenced the room, then lands after its resume [then] it raises no alarm and the next reopen resumes the room
// [if] a reopen suspend fails after it silenced the room [then] the next reopen still resumes it (control)
// [if] two reopens overlap and the later one's suspend lands after the earlier one resumed, then its resume fails [then] the next reopen resumes the room
// [if] the MAIN shown never reached the sink (its move and a newer pick both failed) [then] picking it again moves the sink rather than skipping as a repeat
// [if] no landing is known yet in this mount (a restored pick) [then] a repeat pick still skips setSinkId (control)
// [if] a repeat pick's monitor setup throws [then] it rejects with that error and the live MAIN route and menu stand; a first pick's setup failure still fails the route (control)
// [if] a repeat pick's re-assert and reopen both fail [then] each failure is recorded under its own event and both are shown
// [if] a room reopen fails [then] the Audio I/O Test refuses MAIN and AUDIO IN until a reopen succeeds or the routes are torn down
// [if] a retired route's reopen lands clean after the replacement route's reopen failed [then] the replacement's retry error and Test refusal stay
// [if] the Mac shell releases the MAIN pin while a MAIN pick is still reopening [then] the pick is retired and never republishes the released device
// [if] the Mac shell releases a MAIN pin macOS kept overriding [then] the room is reopened, counted as MAIN switching, and a failed reopen shows the retry error so MAIN and AUDIO IN Tests refuse
// [if] the Mac shell reasserts the MAIN pin after macOS moved the default [then] the room is reopened, counted as MAIN switching, and a failed reopen shows the retry error unless a newer pick owns MAIN, and a later clean reopen clears it
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SPEAKERS = 'native:BuiltInSpeakerDevice';
const USB = 'native:UsbInterface';

let failCommands = 0;
/** While above zero, the next command replies are held here until released. */
let holdCommands = 0;
const heldReplies = [];
/** Every native set_master uid, in send order. */
const setMasterUids = [];

class ReplyWorker {
	constructor() {
		this.onmessage = null;
		this.onerror = null;
		ReplyWorker.last = this;
	}
	postMessage(message) {
		queueMicrotask(() => {
			if (this.onmessage === null) return;
			if (message.kind === 'connect') {
				this.onmessage({ data: { kind: 'open' } });
				return;
			}
			if (message.kind !== 'command') return;
			if (message.payload.type === 'set_master') setMasterUids.push(message.payload.uid);
			if (holdCommands > 0) {
				holdCommands -= 1;
				heldReplies.push(() =>
					this.onmessage?.({ data: { kind: 'message', payload: { type: 'ok', id: message.payload.id } } })
				);
				return;
			}
			if (failCommands > 0) {
				failCommands -= 1;
				this.onmessage({
					data: {
						kind: 'message',
						payload: { type: 'error', id: message.payload.id, message: 'set_master refused' }
					}
				});
				return;
			}
			this.onmessage({ data: { kind: 'message', payload: { type: 'ok', id: message.payload.id } } });
		});
	}
	terminate() {}
}

function roomContext() {
	const calls = [];
	const listeners = new Set();
	const ctx = { currentTime: 0, sampleRate: 48000, state: 'running', destination: null, calls };
	ctx.addEventListener = (type, fn) => type === 'statechange' && listeners.add(fn);
	ctx.removeEventListener = (type, fn) => type === 'statechange' && listeners.delete(fn);
	ctx.setState = (state) => {
		ctx.state = state;
		for (const fn of [...listeners]) fn();
	};
	ctx.suspend = async () => {
		calls.push('suspend');
		ctx.setState('suspended');
	};
	ctx.resume = async () => {
		calls.push('resume');
		ctx.setState('running');
	};
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

let audio;

/** Collect perf-event console lines while `fn` runs. */
async function captureWarnings(fn) {
	const original = console.warn;
	const lines = [];
	console.warn = (line) => lines.push(String(line));
	try {
		await fn();
	} finally {
		console.warn = original;
	}
	return lines;
}

/** Run `fn` with no Mac shell sink, so MAIN moves through setSinkId. */
async function onBrowserPath(fn) {
	const sink = globalThis.OPENDJ_CUE_SINK;
	const nav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
	delete globalThis.OPENDJ_CUE_SINK;
	Object.defineProperty(globalThis, 'navigator', {
		value: { mediaDevices: { enumerateDevices: async () => [] } },
		configurable: true
	});
	try {
		await fn();
	} finally {
		globalThis.OPENDJ_CUE_SINK = sink;
		if (nav === undefined) delete globalThis.navigator;
		else Object.defineProperty(globalThis, 'navigator', nav);
	}
}

async function waitFor(predicate, what) {
	for (let i = 0; !predicate(); i++) {
		if (i >= 500) assert.fail(`timed out waiting for ${what}`);
		await new Promise((r) => setTimeout(r, 1));
	}
}

let store;

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
	audio = await loadTypeScriptModule('tests/unit/fixtures/master-switch-entry.ts');
});

beforeEach(() => {
	failCommands = 0;
	holdCommands = 0;
	heldReplies.length = 0;
	audio.disposeHeadphoneMonitor();
	const hp = audio.mixerState.headphones;
	hp.outputs = [
		{ id: SPEAKERS, label: 'MacBook Pro Speakers' },
		{ id: USB, label: 'USB Interface' }
	];
	hp.output_mode = 'practice';
	hp.split_reason = null;
	hp.selected_output_device_id = null;
	hp.selected_master_output_device_id = null;
	hp.routes.master = { state: 'default', selected: false };
	hp.error = null;
});

test('a MAIN pick reopens the room stream after the sink moves', async () => {
	const ctx = roomContext();
	const source = () => ({ context: ctx, masterGain: ctx.createGain() });
	await audio.selectMasterOutput(USB, source);
	assert.deepEqual(ctx.calls, ['suspend', 'resume']);
	assert.equal(ctx.state, 'running');
	assert.equal(audio.mixerState.headphones.selected_master_output_device_id, USB);
	assert.equal(audio.mixerState.headphones.routes.master.state, 'selected');
	assert.equal(audio.mixerState.headphones.error, null);
});

test('picking the MAIN already shown reopens the stream and never fails the route', async () => {
	const ctx = roomContext();
	const source = () => ({ context: ctx, masterGain: ctx.createGain() });
	await audio.selectMasterOutput(USB, source);
	ctx.calls.length = 0;
	await audio.selectMasterOutput(USB, source);
	assert.deepEqual(ctx.calls, ['suspend', 'resume'], 'a repeat pick must reopen the stream, not no-op');

	// Control: a shell that refuses the re-assert leaves MAIN selected with a
	// visible error, never the failed state that stranded the menu.
	ctx.calls.length = 0;
	failCommands = 1;
	await audio.selectMasterOutput(USB, source);
	assert.equal(audio.mixerState.headphones.selected_master_output_device_id, USB);
	assert.equal(audio.mixerState.headphones.routes.master.state, 'selected');
	assert.match(audio.mixerState.headphones.error, /Pick the device again/);
});

test("a repeat pick whose monitor setup throws keeps the live MAIN route (Sol P1 on #6835)", async () => {
	const ctx = roomContext();
	await audio.selectMasterOutput(USB, () => ({ context: ctx, masterGain: ctx.createGain() }));
	const broken = () => {
		throw new Error('engine graph gone');
	};
	await assert.rejects(audio.selectMasterOutput(USB, broken), /engine graph gone/);
	assert.equal(audio.mixerState.headphones.selected_master_output_device_id, USB);
	assert.equal(audio.mixerState.headphones.routes.master.state, 'selected', 'a re-pick never fails the live route');
	// Control: a first pick of another device whose setup throws still fails.
	await assert.rejects(audio.selectMasterOutput(SPEAKERS, broken), /engine graph gone/);
	assert.equal(audio.mixerState.headphones.routes.master.state, 'failed');
	assert.equal(audio.mixerState.headphones.selected_master_output_device_id, USB, 'the failed pick rolls the menu back');
});

test("a repeat pick whose re-assert and reopen both fail records each failure (Sol P1 on #6835)", async () => {
	const ctx = roomContext();
	const source = () => ({ context: ctx, masterGain: ctx.createGain() });
	await audio.selectMasterOutput(USB, source);
	ctx.resume = async () => {
		throw new Error('resume died');
	};
	failCommands = 1;
	const original = console.error;
	const lines = [];
	console.error = (line) => lines.push(String(line));
	try {
		await audio.selectMasterOutput(USB, source);
	} finally {
		console.error = original;
	}
	assert.ok(lines.some((l) => l.includes('master-output-reassert-failed') && l.includes('set_master refused')), lines.join('\n'));
	assert.ok(lines.some((l) => l.includes('master-output-reopen-failed') && l.includes('resume died')), lines.join('\n'));
	assert.ok(!lines.some((l) => l.includes('master-output-reopen-failed') && l.includes('set_master refused')), 'the re-assert is not filed as a reopen failure');
	const shown = audio.mixerState.headphones.error ?? '';
	assert.match(shown, /set_master refused/);
	assert.match(shown, /resume died/);
	assert.equal(audio.mixerState.headphones.routes.master.state, 'selected');
});

test('an older pick that fails after a newer one started does not roll back', async () => {
	const ctx = roomContext();
	const source = () => ({ context: ctx, masterGain: ctx.createGain() });
	await audio.selectMasterOutput(SPEAKERS, source);
	failCommands = 1;
	const older = audio.selectMasterOutput(USB, source);
	const newer = audio.selectMasterOutput(SPEAKERS, source);
	await assert.doesNotReject(older);
	await newer;
	assert.equal(audio.mixerState.headphones.selected_master_output_device_id, SPEAKERS);
	assert.notEqual(audio.mixerState.headphones.routes.master.state, 'failed');

	// Control: the same failure with no newer pick still rolls back and rejects.
	failCommands = 1;
	await assert.rejects(audio.selectMasterOutput(USB, source), /master output selection failed/);
	assert.equal(audio.mixerState.headphones.selected_master_output_device_id, SPEAKERS);
	assert.equal(audio.mixerState.headphones.routes.master.state, 'failed');
});

test("an older pick's late reopen failure never overwrites a newer pick's result", async () => {
	const ctx = roomContext();
	const source = () => ({ context: ctx, masterGain: ctx.createGain() });
	await audio.selectMasterOutput(SPEAKERS, source);
	let rejectOlder;
	let resumes = 0;
	ctx.resume = () => {
		resumes += 1;
		ctx.calls.push('resume');
		if (resumes === 1) return new Promise((_, reject) => (rejectOlder = reject));
		ctx.state = 'running';
		return Promise.resolve();
	};
	const warned = await captureWarnings(async () => {
		const older = audio.selectMasterOutput(USB, source);
		await waitFor(() => rejectOlder !== undefined, 'the older pick to reach resume');
		await audio.selectMasterOutput(SPEAKERS, source);
		assert.equal(audio.mixerState.headphones.error, null);
		rejectOlder(new Error('older resume died'));
		await older;
	});
	assert.equal(audio.mixerState.headphones.error, null, 'the stale pick must not publish its failure');
	assert.equal(audio.mixerState.headphones.selected_master_output_device_id, SPEAKERS);
	assert.equal(ctx.state, 'running', 'the newer pick must resume the room the older pick suspended');
	assert.ok(
		warned.some((line) => line.includes('master-output-select-superseded-failure') && line.includes('older resume died')),
		'the stale reopen failure must be recorded'
	);
});

test('a room context that cannot be reopened shows a retry error', async () => {
	for (const strip of [
		(ctx) => {
			delete ctx.suspend;
			delete ctx.resume;
		},
		(ctx) => {
			ctx.suspend = async () => {
				ctx.state = 'closed';
			};
		}
	]) {
		audio.disposeHeadphoneMonitor();
		audio.mixerState.headphones.selected_master_output_device_id = null;
		audio.mixerState.headphones.error = null;
		const ctx = roomContext();
		strip(ctx);
		await audio.selectMasterOutput(USB, () => ({ context: ctx, masterGain: ctx.createGain() }));
		assert.match(audio.mixerState.headphones.error ?? '', /Pick the device again/);
		assert.equal(audio.mixerState.headphones.routes.master.state, 'selected', 'the pin itself landed');
		assert.equal(audio.mainReopenFailed(), true, 'the Test refuses until a reopen succeeds');
	}
	// Control: a context that reopens leaves no error, and a clean reopen lifts
	// the Test refusal (a teardown does too).
	audio.disposeHeadphoneMonitor();
	assert.equal(audio.mainReopenFailed(), false, 'a teardown forgets the failed reopen');
	audio.mixerState.headphones.selected_master_output_device_id = null;
	const ok = roomContext();
	await audio.selectMasterOutput(USB, () => ({ context: ok, masterGain: ok.createGain() }));
	assert.equal(audio.mixerState.headphones.error, null);
	assert.equal(audio.mainReopenFailed(), false);
});

test('a clean repeat pick clears the retry error a failed reopen left', async () => {
	const ctx = roomContext();
	const source = () => ({ context: ctx, masterGain: ctx.createGain() });
	const resume = ctx.resume;
	ctx.resume = async () => {
		throw new Error('resume died');
	};
	await audio.selectMasterOutput(USB, source);
	assert.match(audio.mixerState.headphones.error ?? '', /Pick the device again/);
	assert.equal(ctx.state, 'suspended', 'the failed reopen really leaves the room suspended');
	assert.equal(audio.mainReopenFailed(), true, 'precondition: the Test refuses after the failed reopen');
	ctx.calls.length = 0;
	ctx.resume = resume;
	await audio.selectMasterOutput(USB, source);
	assert.deepEqual(ctx.calls, ['resume'], 'the retry must resume the room the failed reopen suspended');
	assert.equal(ctx.state, 'running');
	assert.equal(audio.mixerState.headphones.error, null);
	assert.equal(audio.mainReopenFailed(), false, 'the clean reopen lifts the Test refusal');
});

test('a room context already suspended at the pick is left suspended', async () => {
	const ctx = roomContext();
	ctx.state = 'suspended';
	await audio.selectMasterOutput(USB, () => ({ context: ctx, masterGain: ctx.createGain() }));
	assert.deepEqual(ctx.calls, [], 'a paused or autoplay-blocked context must not be resumed');
	assert.equal(ctx.state, 'suspended');
	assert.equal(audio.mixerState.headphones.error, null);
	assert.equal(audio.mixerState.headphones.selected_master_output_device_id, USB);

	// A room this module reopened cleanly is not "ours" any more: when the app
	// later pauses it, the next pick leaves that pause alone too.
	const reopened = roomContext();
	// No statechange events here, so the mark must clear on its own resume.
	delete reopened.addEventListener;
	delete reopened.removeEventListener;
	const source = () => ({ context: reopened, masterGain: reopened.createGain() });
	await audio.selectMasterOutput(SPEAKERS, source);
	assert.equal(reopened.state, 'running');
	await reopened.suspend();
	reopened.calls.length = 0;
	await audio.selectMasterOutput(USB, source);
	assert.deepEqual(reopened.calls, [], 'a pause after a clean reopen must not be resumed');
	assert.equal(reopened.state, 'suspended');
});

test('a teardown during the reopen stops the pick from publishing', async () => {
	const ctx = roomContext();
	const source = () => ({ context: ctx, masterGain: ctx.createGain() });
	let finishResume;
	ctx.resume = () => {
		ctx.calls.push('resume');
		return new Promise((resolve) => (finishResume = resolve));
	};
	const pick = audio.selectMasterOutput(USB, source);
	await waitFor(() => finishResume !== undefined, 'the pick to reach resume');
	audio.disposeHeadphoneMonitor();
	audio.mixerState.headphones.selected_master_output_device_id = null;
	finishResume();
	await pick.catch(() => {});
	assert.equal(audio.mixerState.headphones.selected_master_output_device_id, null);
});

test('a repeat pick during a pending sink move moves the sink itself (browser path)', async () => {
	await onBrowserPath(async () => {
		const ctx = roomContext();
		const sinkCalls = [];
		let rejectFirst;
		ctx.setSinkId = (id) => {
			sinkCalls.push(id);
			if (sinkCalls.length === 2) return new Promise((_, reject) => (rejectFirst = reject));
			return Promise.resolve();
		};
		const source = () => ({ context: ctx, masterGain: ctx.createGain() });
		await audio.selectMasterOutput(SPEAKERS, source);
		const warned = await captureWarnings(async () => {
			const first = audio.selectMasterOutput(USB, source);
			await waitFor(() => rejectFirst !== undefined, 'the first pick to reach setSinkId');
			assert.equal(audio.outputSwitchesPending().master, true, 'a pending MAIN move is reported to the Audio I/O Test');
			await audio.selectMasterOutput(USB, source);
			assert.equal(sinkCalls.length, 3, 'the repeat pick must call setSinkId, not trust the pending move');
			rejectFirst(new Error('setSinkId refused'));
			await assert.doesNotReject(first);
			assert.equal(audio.outputSwitchesPending().master, false, 'a settled move is no longer pending');
		});
		assert.equal(audio.mixerState.headphones.selected_master_output_device_id, USB);
		assert.equal(audio.mixerState.headphones.routes.master.state, 'selected');
		assert.equal(ctx.state, 'running');
		assert.ok(
			warned.some((line) => line.includes('master-output-select-superseded-failure')),
			'the stale failure must be recorded, not swallowed'
		);

		// Control: with no move pending, a repeat pick still skips setSinkId.
		sinkCalls.length = 0;
		await audio.selectMasterOutput(USB, source);
		assert.equal(sinkCalls.length, 0);
	});
});

test("an older pick's late sink move never overwrites a newer pick's failed route (browser path)", async () => {
	await onBrowserPath(async () => {
		const ctx = roomContext();
		let finishOlder;
		let calls = 0;
		ctx.setSinkId = () => {
			calls += 1;
			if (calls === 2) return new Promise((resolve) => (finishOlder = resolve));
			if (calls === 3) return Promise.reject(new Error('newer setSinkId refused'));
			return Promise.resolve();
		};
		const source = () => ({ context: ctx, masterGain: ctx.createGain() });
		await audio.selectMasterOutput(SPEAKERS, source);
		const older = audio.selectMasterOutput(USB, source);
		await waitFor(() => finishOlder !== undefined, 'the older pick to reach setSinkId');
		await assert.rejects(audio.selectMasterOutput(SPEAKERS, source), /master output selection failed/);
		assert.equal(audio.mixerState.headphones.routes.master.state, 'failed');
		finishOlder();
		await older;
		assert.equal(
			audio.mixerState.headphones.routes.master.state,
			'failed',
			'the superseded pick must not publish its route'
		);
		// Overshoot control: no newer device was ever applied, so nothing is
		// re-asserted over the failed route.
		await new Promise((r) => setTimeout(r, 5));
		assert.equal(calls, 3);
	});
});

test('retrying a MAIN whose move never landed moves the sink instead of skipping it as a repeat (browser path)', async () => {
	await onBrowserPath(async () => {
		const THIRD = 'native:ThirdInterface';
		audio.mixerState.headphones.outputs = [...audio.mixerState.headphones.outputs, { id: THIRD, label: 'Third Interface' }];
		const ctx = roomContext();
		const sinkCalls = [];
		let failB;
		ctx.setSinkId = (id) => {
			sinkCalls.push(id);
			if (sinkCalls.length === 2) return new Promise((_, reject) => (failB = reject));
			if (sinkCalls.length === 3) return Promise.reject(new Error('third setSinkId refused'));
			return Promise.resolve();
		};
		const source = () => ({ context: ctx, masterGain: ctx.createGain() });
		await audio.selectMasterOutput(SPEAKERS, source);
		const pickB = audio.selectMasterOutput(USB, source);
		await waitFor(() => failB !== undefined, 'the USB pick to reach setSinkId');
		await assert.rejects(audio.selectMasterOutput(THIRD, source), /master output selection failed/);
		assert.equal(audio.mixerState.headphones.selected_master_output_device_id, USB, 'the failed pick rolls the menu back to USB');
		await captureWarnings(async () => {
			failB(new Error('usb setSinkId refused'));
			await pickB;
		});
		// The sink is still on the speakers while the menu shows USB.
		sinkCalls.length = 0;
		await audio.selectMasterOutput(USB, source);
		assert.equal(sinkCalls.length, 1, 'the retry must apply the sink, not trust the menu');
		assert.equal(audio.mixerState.headphones.routes.master.state, 'selected');
		// Control: once it landed, the same pick again is a repeat and skips setSinkId.
		sinkCalls.length = 0;
		await audio.selectMasterOutput(USB, source);
		assert.equal(sinkCalls.length, 0);
	});
});

test('a MAIN pick with no landing known since a remount moves the sink to the shown device, then repeats skip (browser path)', async () => {
	await onBrowserPath(async () => {
		const ctx = roomContext();
		const sinkCalls = [];
		ctx.setSinkId = (id) => (sinkCalls.push(id), Promise.resolve());
		const source = () => ({ context: ctx, masterGain: ctx.createGain() });
		// beforeEach disposed the monitor, so no move has landed in this mount:
		// the menu still shows USB while the new room plays on the OS default.
		audio.mixerState.headphones.selected_master_output_device_id = USB;
		audio.mixerState.headphones.routes.master = { state: 'selected', selected: true };
		await audio.selectMasterOutput(USB, source);
		assert.equal(sinkCalls.length, 1, 'an unknown landing must move the sink, not trust the menu');
		assert.equal(audio.mixerState.headphones.routes.master.state, 'selected');
		// Control: once that move landed, the same pick again is a repeat.
		sinkCalls.length = 0;
		await audio.selectMasterOutput(USB, source);
		assert.equal(sinkCalls.length, 0, 'a known landing keeps the OUT-change-lag skip');
	});
});

test("an older pick's sink move that lands after a newer pick's is put back on the device shown (browser path)", async () => {
	await onBrowserPath(async () => {
		const ctx = roomContext();
		let finishOlder;
		const sinkIds = [];
		ctx.setSinkId = (id) => {
			sinkIds.push(id);
			if (sinkIds.length === 2) return new Promise((resolve) => (finishOlder = resolve));
			return Promise.resolve();
		};
		const source = () => ({ context: ctx, masterGain: ctx.createGain() });
		await audio.selectMasterOutput(SPEAKERS, source);
		const older = audio.selectMasterOutput(USB, source);
		await waitFor(() => finishOlder !== undefined, 'the older pick to reach setSinkId');
		await audio.selectMasterOutput(SPEAKERS, source);
		assert.deepEqual(sinkIds, [SPEAKERS, USB, SPEAKERS]);
		ctx.calls.length = 0;
		finishOlder();
		await older;
		await waitFor(() => sinkIds.length === 4, 'the shown MAIN to be re-asserted');
		assert.equal(sinkIds[3], SPEAKERS, 'the room must end on the device the menu shows');
		// The corrective move reopens the room too: repeated moves are what
		// leave a context running on a dead stream.
		await waitFor(() => ctx.calls.includes('resume'), 'the room to be reopened after the corrective move');
		assert.deepEqual(ctx.calls, ['suspend', 'resume']);
		assert.equal(ctx.state, 'running');
		assert.equal(audio.mixerState.headphones.selected_master_output_device_id, SPEAKERS);
		assert.equal(audio.mixerState.headphones.routes.master.state, 'selected');
	});
});

test('a corrective sink move follows a pick that lands while it is pending (browser path)', async () => {
	await onBrowserPath(async () => {
		const ctx = roomContext();
		const pending = [];
		const sinkIds = [];
		ctx.setSinkId = (id) => {
			sinkIds.push(id);
			// The older pick's move and the correction both wait for the test.
			if (sinkIds.length === 2 || sinkIds.length === 4) return new Promise((resolve) => pending.push(resolve));
			return Promise.resolve();
		};
		const source = () => ({ context: ctx, masterGain: ctx.createGain() });
		await audio.selectMasterOutput(SPEAKERS, source);
		const older = audio.selectMasterOutput(USB, source);
		await waitFor(() => pending.length === 1, 'the older pick to reach setSinkId');
		await audio.selectMasterOutput(SPEAKERS, source);
		pending[0]();
		await older;
		await waitFor(() => pending.length === 2, 'the correction to reach setSinkId');
		assert.equal(sinkIds[3], SPEAKERS);
		// A third pick lands while the correction is pending.
		await audio.selectMasterOutput(USB, source);
		assert.equal(sinkIds[4], USB);
		pending[1]();
		await waitFor(() => sinkIds.length === 6, 'the correction to follow the newest pick');
		assert.equal(sinkIds[5], USB, 'a stale correction must not leave the room on the older device');
		assert.equal(audio.mixerState.headphones.selected_master_output_device_id, USB);
	});
});

test('a corrective sink move keeps following newer picks until the sink matches the menu (browser path)', async () => {
	await onBrowserPath(async () => {
		const ctx = roomContext();
		const pending = [];
		const sinkIds = [];
		ctx.setSinkId = (id) => {
			sinkIds.push(id);
			// The older pick's move and every correction pass up to the last wait for the test.
			const n = sinkIds.length;
			if (n === 2 || (n >= 4 && n <= 12 && n % 2 === 0)) return new Promise((resolve) => pending.push(resolve));
			return Promise.resolve();
		};
		const source = () => ({ context: ctx, masterGain: ctx.createGain() });
		await audio.selectMasterOutput(SPEAKERS, source);
		const older = audio.selectMasterOutput(USB, source);
		await waitFor(() => pending.length === 1, 'the older pick to reach setSinkId');
		await audio.selectMasterOutput(SPEAKERS, source);
		pending[0]();
		await older;
		await waitFor(() => pending.length === 2, 'the correction to reach setSinkId');
		// Five newer picks, each landing while a correction pass is pending.
		let shown = SPEAKERS;
		for (let pick = 0; pick < 5; pick += 1) {
			shown = shown === USB ? SPEAKERS : USB;
			await audio.selectMasterOutput(shown, source);
			pending[pending.length - 1]();
			if (pick < 4) await waitFor(() => pending.length === pick + 3, 'the correction to follow the newest pick');
		}
		await waitFor(() => sinkIds.length === 14, 'the last correction pass');
		await waitFor(() => ctx.calls.includes('resume'), 'the room to be reopened');
		assert.equal(audio.mixerState.headphones.selected_master_output_device_id, shown);
		assert.equal(sinkIds.at(-1), shown, 'the correction must not settle on a stale target');
	});
});

test('a correction deferred behind a still-pending move runs once that move fails (browser path)', async () => {
	await onBrowserPath(async () => {
		const THIRD = 'native:ThirdDevice';
		audio.mixerState.headphones.outputs.push({ id: THIRD, label: 'Third Device' });
		const ctx = roomContext();
		const pending = new Map();
		const sinkIds = [];
		ctx.setSinkId = (id) => {
			sinkIds.push(id);
			// The USB and THIRD picks wait for the test; every other move lands at once.
			if (sinkIds.length > 1 && (String(id).includes('Usb') || String(id).includes('Third')) && !pending.has(id)) {
				return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
			}
			return Promise.resolve();
		};
		const source = () => ({ context: ctx, masterGain: ctx.createGain() });
		await audio.selectMasterOutput(SPEAKERS, source);
		const toUsb = audio.selectMasterOutput(USB, source);
		const toThird = audio.selectMasterOutput(THIRD, source);
		await waitFor(() => pending.size === 2, 'both older picks to reach setSinkId');
		await audio.selectMasterOutput(SPEAKERS, source);
		// The oldest move lands while THIRD's is still pending, so its correction waits.
		pending.get(USB).resolve();
		await toUsb;
		const before = sinkIds.length;
		// Then THIRD's move fails: nothing else would bring the room back.
		pending.get(THIRD).reject(new Error('third setSinkId refused'));
		await toThird.catch(() => {});
		await waitFor(() => sinkIds.length > before, 'the deferred correction to run');
		assert.equal(sinkIds.at(-1), SPEAKERS, 'the room must not be left on the stale USB device');
		assert.equal(audio.mixerState.headphones.selected_master_output_device_id, SPEAKERS);
	});
});

test('a teardown while a corrective sink move is pending stops the correction (browser path)', async () => {
	await onBrowserPath(async () => {
		const ctx = roomContext();
		let finishOlder;
		let finishCorrection;
		const sinkIds = [];
		ctx.setSinkId = (id) => {
			sinkIds.push(id);
			if (sinkIds.length === 2) return new Promise((resolve) => (finishOlder = resolve));
			if (sinkIds.length === 4) return new Promise((resolve) => (finishCorrection = resolve));
			return Promise.resolve();
		};
		const source = () => ({ context: ctx, masterGain: ctx.createGain() });
		await audio.selectMasterOutput(SPEAKERS, source);
		const older = audio.selectMasterOutput(USB, source);
		await waitFor(() => finishOlder !== undefined, 'the older pick to reach setSinkId');
		await audio.selectMasterOutput(SPEAKERS, source);
		finishOlder();
		await older;
		await waitFor(() => finishCorrection !== undefined, 'the corrective move to start');
		audio.disposeHeadphoneMonitor();
		// The routes that replace the torn-down ones show another device: a
		// correction that outlived its routes would chase it.
		const hp = audio.mixerState.headphones;
		hp.selected_master_output_device_id = USB;
		hp.routes.master = { state: 'selected', selected: true };
		hp.error = null;
		ctx.calls.length = 0;
		finishCorrection();
		await new Promise((r) => setTimeout(r, 20));
		assert.deepEqual(sinkIds, [SPEAKERS, USB, SPEAKERS, SPEAKERS], 'no move after teardown');
		assert.deepEqual(ctx.calls, [], 'no reopen after teardown');
		assert.equal(hp.error, null);
	});
});

test("a teardown during a correction's reopen keeps its failure off the new routes (browser path)", async () => {
	await onBrowserPath(async () => {
		const ctx = roomContext();
		let finishOlder;
		const sinkIds = [];
		ctx.setSinkId = (id) => {
			sinkIds.push(id);
			if (sinkIds.length === 2) return new Promise((resolve) => (finishOlder = resolve));
			return Promise.resolve();
		};
		const source = () => ({ context: ctx, masterGain: ctx.createGain() });
		await audio.selectMasterOutput(SPEAKERS, source);
		const older = audio.selectMasterOutput(USB, source);
		await waitFor(() => finishOlder !== undefined, 'the older pick to reach setSinkId');
		await audio.selectMasterOutput(SPEAKERS, source);
		let failResume;
		ctx.resume = () => {
			ctx.calls.push('resume');
			return new Promise((_, reject) => (failResume = reject));
		};
		finishOlder();
		await older;
		await waitFor(() => failResume !== undefined, "the correction's reopen to reach resume");
		audio.disposeHeadphoneMonitor();
		const hp = audio.mixerState.headphones;
		hp.routes.master = { state: 'selected', selected: true };
		hp.error = null;
		failResume(new Error('resume refused'));
		await new Promise((r) => setTimeout(r, 20));
		assert.equal(hp.error, null, 'a torn-down correction must not report on the new routes');
		assert.equal(hp.routes.master.state, 'selected');
	});
});

test('a correction with no teardown still reopens the room once its move lands (browser path)', async () => {
	await onBrowserPath(async () => {
		const ctx = roomContext();
		let finishOlder;
		let finishCorrection;
		const sinkIds = [];
		ctx.setSinkId = (id) => {
			sinkIds.push(id);
			if (sinkIds.length === 2) return new Promise((resolve) => (finishOlder = resolve));
			if (sinkIds.length === 4) return new Promise((resolve) => (finishCorrection = resolve));
			return Promise.resolve();
		};
		const source = () => ({ context: ctx, masterGain: ctx.createGain() });
		await audio.selectMasterOutput(SPEAKERS, source);
		const older = audio.selectMasterOutput(USB, source);
		await waitFor(() => finishOlder !== undefined, 'the older pick to reach setSinkId');
		await audio.selectMasterOutput(SPEAKERS, source);
		finishOlder();
		await older;
		await waitFor(() => finishCorrection !== undefined, 'the corrective move to start');
		ctx.calls.length = 0;
		finishCorrection();
		await waitFor(() => ctx.calls.includes('resume'), 'the room to be reopened');
		assert.deepEqual(ctx.calls, ['suspend', 'resume']);
	});
});

test("a repeat pick's OS-default re-assert that lands after a newer pick is put back on the device shown", async () => {
	const ctx = roomContext();
	const source = () => ({ context: ctx, masterGain: ctx.createGain() });
	await audio.selectMasterOutput(USB, source);
	setMasterUids.length = 0;
	holdCommands = 1;
	const repeat = audio.selectMasterOutput(USB, source);
	await waitFor(() => heldReplies.length === 1, "the repeat pick's set_master to be sent");
	await audio.selectMasterOutput(SPEAKERS, source);
	assert.deepEqual(setMasterUids, ['UsbInterface', 'BuiltInSpeakerDevice']);
	// The repeat's set_master lands last: the OS default is back on USB.
	heldReplies.shift()();
	await repeat;
	await waitFor(() => setMasterUids.length === 3, 'the shown MAIN to be re-asserted');
	assert.equal(setMasterUids[2], 'BuiltInSpeakerDevice', 'the room must end on the device the menu shows');
	assert.equal(audio.mixerState.headphones.selected_master_output_device_id, SPEAKERS);
	assert.equal(audio.mixerState.headphones.routes.master.state, 'selected');
});

test('control: a repeat pick with no newer pick re-asserts once and reopens', async () => {
	const ctx = roomContext();
	const source = () => ({ context: ctx, masterGain: ctx.createGain() });
	await audio.selectMasterOutput(USB, source);
	setMasterUids.length = 0;
	ctx.calls.length = 0;
	await audio.selectMasterOutput(USB, source);
	await new Promise((r) => setTimeout(r, 10));
	assert.deepEqual(setMasterUids, ['UsbInterface']);
	assert.deepEqual(ctx.calls, ['suspend', 'resume']);
	assert.equal(audio.mixerState.headphones.routes.master.state, 'selected');
});

test("a pick a teardown retired never rolls back the MAIN route a new mount published (browser path)", async () => {
	await onBrowserPath(async () => {
		const hp = audio.mixerState.headphones;
		for (const [outcome, mounted] of [['fails', SPEAKERS], ['lands', SPEAKERS], ['fails', USB]]) {
			audio.disposeHeadphoneMonitor();
			hp.selected_master_output_device_id = null;
			hp.routes.master = { state: 'default', selected: false };
			const ctx = roomContext();
			let finish;
			ctx.setSinkId = () => new Promise((resolve, reject) => (finish = { resolve, reject }));
			const source = () => ({ context: ctx, masterGain: ctx.createGain() });
			const pick = audio.selectMasterOutput(USB, source);
			await waitFor(() => finish !== undefined, 'the pick to reach setSinkId');
			// /performance unmounts, and the next mount publishes its own MAIN
			// (which may restore the very device the retired pick was moving to).
			audio.disposeHeadphoneMonitor();
			hp.selected_master_output_device_id = mounted;
			hp.routes.master = { state: 'selected', selected: true };
			if (outcome === 'fails') finish.reject(new Error('old setSinkId refused'));
			else finish.resolve();
			await assert.rejects(pick, /stale headphone operation/);
			assert.equal(hp.selected_master_output_device_id, mounted, `a retired pick that ${outcome} must not roll back the new MAIN`);
			assert.equal(hp.routes.master.state, 'selected', `a retired pick that ${outcome} must not fail the new route`);
		}
	});
});

test('control: a pick a teardown retired with no new route still undoes its own menu write (browser path)', async () => {
	await onBrowserPath(async () => {
		const hp = audio.mixerState.headphones;
		const ctx = roomContext();
		let reject;
		ctx.setSinkId = () => new Promise((_, r) => (reject = r));
		const source = () => ({ context: ctx, masterGain: ctx.createGain() });
		const pick = audio.selectMasterOutput(USB, source);
		await waitFor(() => reject !== undefined, 'the pick to reach setSinkId');
		assert.equal(hp.selected_master_output_device_id, USB, 'precondition: the optimistic write');
		audio.disposeHeadphoneMonitor();
		reject(new Error('old setSinkId refused'));
		await assert.rejects(pick, /stale headphone operation/);
		assert.equal(hp.selected_master_output_device_id, null, 'nothing the retired pick started is left shown');
	});
});

test("a retired pick's late browser sink move is never corrected, reopened or reported through its dead room (browser path)", async () => {
	await onBrowserPath(async () => {
		const hp = audio.mixerState.headphones;
		const oldCtx = roomContext();
		const oldSinks = [];
		let finishOld;
		oldCtx.setSinkId = (id) => {
			oldSinks.push(id);
			return oldSinks.length === 1 ? new Promise((resolve) => (finishOld = resolve)) : Promise.resolve();
		};
		const old = audio.selectMasterOutput(USB, () => ({ context: oldCtx, masterGain: oldCtx.createGain() }));
		await waitFor(() => finishOld !== undefined, 'the old pick to reach setSinkId');
		audio.disposeHeadphoneMonitor();
		// The replacement mount picks its own MAIN in its own room.
		const newCtx = roomContext();
		newCtx.setSinkId = async () => {};
		await audio.selectMasterOutput(SPEAKERS, () => ({ context: newCtx, masterGain: newCtx.createGain() }));
		oldCtx.calls.length = 0;
		newCtx.calls.length = 0;
		finishOld();
		await old.catch(() => {});
		await new Promise((r) => setTimeout(r, 20));
		assert.deepEqual(oldSinks, [USB], 'no correction through the retired room');
		assert.deepEqual(oldCtx.calls, [], 'no reopen of the retired room');
		assert.deepEqual(newCtx.calls, [], "a browser setSinkId on the dead room never moved the new room's output");
		assert.equal(hp.error, null);
		assert.equal(hp.selected_master_output_device_id, SPEAKERS);
		assert.equal(hp.routes.master.state, 'selected');
	});
});

test('a pick refused by validation does not supersede the pick in flight', async () => {
	await onBrowserPath(async () => {
		const ctx = roomContext();
		let finishMove;
		let calls = 0;
		ctx.setSinkId = () => {
			calls += 1;
			if (calls === 2) return new Promise((resolve) => (finishMove = resolve));
			return Promise.resolve();
		};
		const source = () => ({ context: ctx, masterGain: ctx.createGain() });
		await audio.selectMasterOutput(SPEAKERS, source);
		ctx.calls.length = 0;
		const inFlight = audio.selectMasterOutput(USB, source);
		await waitFor(() => finishMove !== undefined, 'the pick to reach setSinkId');
		await assert.rejects(audio.selectMasterOutput('native:NotAnOutput', source));
		finishMove();
		await inFlight;
		assert.deepEqual(ctx.calls, ['suspend', 'resume'], 'the in-flight pick must still reopen');
		assert.equal(ctx.state, 'running');
		assert.equal(audio.mixerState.headphones.selected_master_output_device_id, USB);
		assert.equal(audio.mixerState.headphones.routes.master.state, 'selected');
		assert.equal(audio.mixerState.headphones.error, null);

		// Same again with the refusal landing while the reopen is pending.
		let finishResume;
		const resume = ctx.resume;
		ctx.resume = () => new Promise((resolve) => (finishResume = () => resolve(resume())));
		const reopening = audio.selectMasterOutput(SPEAKERS, source);
		await waitFor(() => finishResume !== undefined, 'the pick to reach resume');
		await assert.rejects(audio.selectMasterOutput('native:NotAnOutput', source));
		finishResume();
		await reopening;
		assert.equal(audio.mixerState.headphones.selected_master_output_device_id, SPEAKERS);
		assert.equal(audio.mixerState.headphones.routes.master.state, 'selected', 'a refusal during the reopen must not leave MAIN failed');
	});
});

test('a room the app resumed and then paused is not resumed by the next pick', async () => {
	const ctx = roomContext();
	const source = () => ({ context: ctx, masterGain: ctx.createGain() });
	const resume = ctx.resume;
	ctx.resume = async () => {
		throw new Error('resume died');
	};
	await audio.selectMasterOutput(USB, source);
	assert.equal(ctx.state, 'suspended', 'precondition: the failed reopen left the room suspended');
	ctx.resume = resume;
	await ctx.resume(); // the app plays
	await ctx.suspend(); // and later pauses on purpose
	ctx.calls.length = 0;
	await audio.selectMasterOutput(SPEAKERS, source);
	assert.deepEqual(ctx.calls, [], "the app's own pause must be left alone");
	assert.equal(ctx.state, 'suspended');
});

/** The reopen module on its own, with a stand-in for the operation timeout. */
async function reopenModule() {
	return loadTypeScriptModule('src/lib/player/headphones-room-reopen.ts');
}
const noTimeout = (_operation, promise) => promise;

test("a reopen whose suspend fails leaves a later pause by the app alone", async () => {
	const reopen = await reopenModule();
	const ctx = roomContext();
	ctx.suspend = async () => {
		ctx.calls.push('suspend');
		throw new Error('suspend refused');
	};
	assert.match(await reopen.reopenRoomOutput(ctx, noTimeout), /suspend refused/);
	assert.equal(ctx.state, 'running', 'precondition: the failed suspend left the room running');
	ctx.setState('suspended'); // the app pauses on purpose
	ctx.calls.length = 0;
	assert.equal(await reopen.reopenRoomOutput(ctx, noTimeout), null);
	assert.deepEqual(ctx.calls, [], "the app's own pause must not be resumed");
	assert.equal(ctx.state, 'suspended');
});

// [if] a reopen suspend times out and lands after every reopen on the room has
// finished [then] the room is resumed at once, never left silent until the next pick
test('a reopen suspend that times out and lands later, with no reopen running, resumes the room at once (Codex/Sol P1 on #6835)', async () => {
	const reopen = await reopenModule();
	const ctx = roomContext();
	let land;
	ctx.suspend = () => {
		ctx.calls.push('suspend');
		return new Promise((resolve) => (land = () => {
			ctx.setState('suspended');
			resolve();
		}));
	};
	const timesOut = (operation, promise) =>
		operation === 'master reopen suspend' ? Promise.reject(new Error('suspend timed out')) : promise;
	assert.match(await reopen.reopenRoomOutput(ctx, timesOut), /timed out/);
	ctx.calls.length = 0;
	land();
	await new Promise((r) => setTimeout(r, 0));
	assert.deepEqual(ctx.calls, ['resume'], 'our own late suspend is undone without waiting for a pick');
	assert.equal(ctx.state, 'running');
	// The silence is no longer ours: the next reopen treats the room as running.
	ctx.suspend = async () => {
		ctx.calls.push('suspend');
		ctx.setState('suspended');
	};
	ctx.calls.length = 0;
	assert.equal(await reopen.reopenRoomOutput(ctx, noTimeout), null);
	assert.deepEqual(ctx.calls, ['suspend', 'resume']);
});

// [if] a late reopen suspend lands while another reopen is running on the room
// [then] that reopen owns the resume; the landing starts none of its own
test('control: a late reopen suspend that lands during another reopen leaves the resume to it', async () => {
	const reopen = await reopenModule();
	const ctx = roomContext();
	const suspends = [];
	ctx.suspend = () => {
		ctx.calls.push('suspend');
		return new Promise((resolve) => suspends.push(resolve));
	};
	const timesOut = (operation, promise) =>
		operation === 'master reopen suspend' ? Promise.reject(new Error('suspend timed out')) : promise;
	assert.match(await reopen.reopenRoomOutput(ctx, timesOut), /timed out/);
	const second = reopen.reopenRoomOutput(ctx, noTimeout);
	await new Promise((r) => setTimeout(r, 0));
	assert.equal(suspends.length, 2, 'precondition: the second reopen is suspending');
	ctx.calls.length = 0;
	// The first suspend lands while the second reopen still runs.
	ctx.setState('suspended');
	suspends[0]();
	await new Promise((r) => setTimeout(r, 0));
	assert.deepEqual(ctx.calls, [], 'no resume of its own while another reopen runs');
	suspends[1]();
	assert.equal(await second, null);
	assert.deepEqual(ctx.calls, ['resume'], 'the running reopen resumes the room once');
	assert.equal(ctx.state, 'running');
});

// [if] the immediate resume after a late landing fails [then] the failure is
// recorded and the room stays marked, so the next pick still resumes it
test('a late landing whose own resume fails records it and leaves the room to the next reopen', async () => {
	const reopen = await loadTypeScriptModule('tests/unit/fixtures/room-reopen-perf-entry.ts');
	const events = [];
	const unsubscribe = reopen.subscribePerfEvents((event) => events.push(event));
	const ctx = roomContext();
	let land;
	ctx.suspend = () => new Promise((resolve) => (land = () => {
		ctx.setState('suspended');
		resolve();
	}));
	const timesOut = (operation, promise) => {
		if (operation === 'master reopen suspend') return Promise.reject(new Error('suspend timed out'));
		if (operation === 'master reopen late resume') return Promise.reject(new Error('late resume timed out'));
		return promise;
	};
	const resume = ctx.resume;
	ctx.resume = () => new Promise(() => {}); // hangs; the timeout above fails it
	assert.match(await reopen.reopenRoomOutput(ctx, timesOut), /timed out/);
	land();
	await new Promise((r) => setTimeout(r, 0));
	assert.equal(ctx.state, 'suspended');
	const recorded = events.filter((e) => e.kind === 'master-reopen-late-resume-failed');
	assert.equal(recorded.length, 1, 'the failed late resume is recorded once');
	assert.match(recorded[0].message, /late resume timed out/);
	assert.equal(recorded[0].severity, 'error');
	unsubscribe();
	ctx.resume = resume;
	ctx.calls.length = 0;
	assert.equal(await reopen.reopenRoomOutput(ctx, noTimeout), null);
	assert.deepEqual(ctx.calls, ['resume'], 'the next reopen still undoes our silence');
});

/** A reopen and a watchdog on one room, sharing the intentional-suspend excuse. */
async function watchedRoom() {
	const mod = await loadTypeScriptModule('tests/unit/fixtures/room-reopen-watchdog-entry.ts');
	const ctx = roomContext();
	const alarms = [];
	const detach = mod.installAudioContextWatchdog(
		ctx,
		{
			pushToast: (message) => alarms.push(message),
			recordPerfTiming() {},
			recordPerfEvent() {},
			sleep: () => new Promise(() => {}),
			noteUnexpectedPause: (state) => alarms.push(`pause:${state}`)
		},
		() => true
	);
	return { mod, ctx, alarms, detach };
}

/** Like watchedRoom, and records each recovery attempt the watchdog starts. */
async function watchedRoomCountingRecoveries() {
	const mod = await loadTypeScriptModule('tests/unit/fixtures/room-reopen-watchdog-entry.ts');
	const ctx = roomContext();
	const alarms = [];
	const recoveries = [];
	const detach = mod.installAudioContextWatchdog(
		ctx,
		{
			pushToast: (message) => alarms.push(message),
			recordPerfTiming() {},
			recordPerfEvent() {},
			sleep: () => {
				recoveries.push(ctx.state);
				return new Promise(() => {});
			},
			noteUnexpectedPause: (state) => alarms.push(`pause:${state}`)
		},
		() => true
	);
	return { mod, ctx, alarms, recoveries, detach };
}

// [if] a device or visibility edge arrives while a reopen suspend that outlived
// its grace is landing [then] the watchdog does not start a recovery that would
// resume our own suspend; a genuine pause with a late suspend pending still recovers
test('a recovery edge while a late reopen suspend lands starts no recovery, and a genuine pause still recovers', async () => {
	const { mod, ctx, alarms, recoveries, detach } = await watchedRoomCountingRecoveries();
	let land;
	ctx.suspend = () => new Promise((resolve) => (land = () => {
		ctx.setState('suspended');
		// A devicechange lands between the browser's state edge and the promise.
		mod.noteRecoveryOpportunity('devicechange');
		resolve();
	}));
	const timesOut = (operation, promise) =>
		operation === 'master reopen suspend' ? Promise.reject(new Error('suspend timed out')) : promise;
	assert.match(await mod.reopenRoomOutput(ctx, timesOut, 10), /timed out/);
	await new Promise((r) => setTimeout(r, 25)); // the grace has ended
	land();
	await new Promise((r) => setTimeout(r, 5));
	assert.deepEqual(alarms, [], 'our own late suspend is not an unexpected pause');
	assert.deepEqual(recoveries, [], 'nor does a recovery edge resume it');
	detach();

	// Control: a late suspend still pending, and a genuine pause the edge finds.
	const room = await watchedRoomCountingRecoveries();
	room.ctx.suspend = () => new Promise(() => {}); // never settles
	assert.match(await room.mod.reopenRoomOutput(room.ctx, timesOut, 10), /timed out/);
	await new Promise((r) => setTimeout(r, 25));
	room.ctx.state = 'suspended'; // paused with no statechange the watchdog saw
	room.mod.noteRecoveryOpportunity('visibilitychange');
	await new Promise((r) => setTimeout(r, 5));
	assert.deepEqual(room.recoveries, ['suspended'], 'a genuine pause still recovers, a task later');
	room.detach();
});

test('a timed-out reopen suspend that lands later while a deck plays raises no unexpected-pause alarm', async () => {
	const { mod, ctx, alarms, detach } = await watchedRoom();
	let land;
	ctx.suspend = () => new Promise((resolve) => (land = () => {
		// As the browser does: the state edge fires, then the promise settles.
		ctx.setState('suspended');
		resolve();
	}));
	const timesOut = (operation, promise) =>
		operation === 'master reopen suspend' ? Promise.reject(new Error('suspend timed out')) : promise;
	assert.match(await mod.reopenRoomOutput(ctx, timesOut), /timed out/);
	land();
	await new Promise((r) => setTimeout(r, 0));
	assert.equal(ctx.state, 'running', 'the late suspend is resumed at once');
	assert.deepEqual(alarms, [], 'our own late suspend is not an unexpected pause');
	// Control: once that suspend has settled the excuse is gone, so a real drop still alarms.
	ctx.setState('running');
	ctx.setState('suspended');
	assert.ok(alarms.includes('pause:suspended'), 'a suspend after ours settled still alarms');
	detach();
});

test('an overlapping reopen whose suspend times out after the first silenced the room keeps it ours until it lands', async () => {
	const { mod, ctx, alarms, detach } = await watchedRoom();
	const pending = { suspend: [], resume: [] };
	ctx.suspend = () => new Promise((resolve, reject) => pending.suspend.push({ resolve, reject }));
	ctx.resume = () => new Promise((resolve, reject) => pending.resume.push({ resolve, reject }));
	const tick = () => new Promise((r) => setTimeout(r, 0));
	let timeOutSecond;
	const secondTimesOut = (operation, promise) =>
		operation === 'master reopen suspend'
			? new Promise((resolve, reject) => {
					timeOutSecond = () => reject(new Error('suspend timed out'));
					promise.then(resolve, () => {});
				})
			: promise;
	const first = mod.reopenRoomOutput(ctx, (_operation, promise) => promise);
	const second = mod.reopenRoomOutput(ctx, secondTimesOut);
	await tick();
	assert.equal(pending.suspend.length, 2, 'precondition: both reopens suspend the running room');
	// The first suspend lands, then the second times out with the room already suspended.
	ctx.setState('suspended');
	pending.suspend[0].resolve();
	await tick();
	timeOutSecond();
	assert.match(await second, /timed out/);
	// The first reopen resumes the room, clearing the shared mark.
	ctx.setState('running');
	pending.resume[0].resolve();
	assert.equal(await first, null);
	// Only now does the second suspend land: still ours, still excused.
	ctx.setState('suspended');
	pending.suspend[1].resolve();
	await tick();
	assert.deepEqual(alarms, [], 'the late suspend of a timed-out overlapping reopen is not an unexpected pause');
	// No reopen is left running, so that landing resumes the room itself.
	assert.equal(pending.resume.length, 2, 'the late landing asks for its own resume');
	ctx.setState('running');
	pending.resume[1].resolve();
	await tick();
	assert.deepEqual(alarms, [], 'and the resume it asked for raises nothing');
	detach();
});

test('a timed-out reopen suspend that never settles gives the excuse back after its grace', async () => {
	const { mod, ctx, alarms, detach } = await watchedRoom();
	ctx.suspend = () => new Promise(() => {}); // never settles
	const timesOut = (operation, promise) =>
		operation === 'master reopen suspend' ? Promise.reject(new Error('suspend timed out')) : promise;
	assert.match(await mod.reopenRoomOutput(ctx, timesOut, 30), /timed out/);
	// Inside the grace a suspend edge is still read as ours.
	await new Promise((r) => setTimeout(r, 5));
	ctx.setState('suspended');
	assert.deepEqual(alarms, [], 'a suspend within the grace is still excused');
	ctx.setState('running');
	await new Promise((r) => setTimeout(r, 50));
	// After it, a genuine pause alarms again (its verdict waits one task for a landing that never comes).
	ctx.setState('suspended');
	await new Promise((r) => setTimeout(r, 5));
	assert.ok(alarms.includes('pause:suspended'), 'a suspend that never settles must not blind the watchdog for good');
	detach();
});

test('a timed-out reopen suspend that lands after its grace is still not an unexpected pause, and only that edge is excused', async () => {
	const { mod, ctx, alarms, detach } = await watchedRoom();
	let land;
	ctx.suspend = () => new Promise((resolve) => (land = () => {
		// As the browser does: the state edge fires, then the promise settles.
		ctx.setState('suspended');
		resolve();
	}));
	const timesOut = (operation, promise) =>
		operation === 'master reopen suspend' ? Promise.reject(new Error('suspend timed out')) : promise;
	assert.match(await mod.reopenRoomOutput(ctx, timesOut, 10), /timed out/);
	await new Promise((r) => setTimeout(r, 25)); // the grace has ended
	land();
	await new Promise((r) => setTimeout(r, 5));
	assert.deepEqual(alarms, [], 'our own suspend landing after the grace is not an unexpected pause');
	// The silence is ours, and no reopen is left to undo it, so it is resumed at once.
	assert.equal(ctx.state, 'running');
	// Control: that landing excused only its own edge; a later genuine pause alarms.
	ctx.setState('suspended');
	await new Promise((r) => setTimeout(r, 5));
	assert.ok(alarms.includes('pause:suspended'), 'a later pause still alarms');
	detach();
});

test('a late reopen suspend whose promise settles before its edge is still not an unexpected pause (Codex P2 BLOCKING on #6835)', async () => {
	const { mod, ctx, alarms, detach } = await watchedRoom();
	let land;
	ctx.suspend = () => new Promise((resolve) => (land = () => {
		// The Web Audio spec order: the state changes and the promise settles,
		// and the statechange event is dispatched in a later task.
		ctx.state = 'suspended';
		resolve();
		setTimeout(() => ctx.setState('suspended'), 2);
	}));
	const timesOut = (operation, promise) =>
		operation === 'master reopen suspend' ? Promise.reject(new Error('suspend timed out')) : promise;
	assert.match(await mod.reopenRoomOutput(ctx, timesOut, 10), /timed out/);
	await new Promise((r) => setTimeout(r, 25)); // the grace has ended
	land();
	await new Promise((r) => setTimeout(r, 10));
	assert.deepEqual(alarms, [], 'the edge our own late suspend caused is not an unexpected pause');
	// Control: that landing excused its own edge only; a later genuine pause alarms.
	ctx.setState('running');
	ctx.setState('suspended');
	await new Promise((r) => setTimeout(r, 5));
	assert.ok(alarms.includes('pause:suspended'), 'a later pause still alarms');
	detach();
});

test("a timed-out suspend's excuse ends once, never taking an overlapping reopen's excuse with it", async () => {
	const { mod, ctx, alarms, detach } = await watchedRoom();
	const pending = [];
	ctx.suspend = () => new Promise((resolve) => pending.push(resolve));
	const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
	const timesOut = (operation, promise) =>
		operation === 'master reopen suspend' ? Promise.reject(new Error('suspend timed out')) : promise;
	assert.match(await mod.reopenRoomOutput(ctx, timesOut, 10), /timed out/);
	const second = mod.reopenRoomOutput(ctx, (_operation, promise) => promise);
	await tick();
	assert.equal(pending.length, 2, 'precondition: both reopens are suspending');
	// The first suspend's grace runs out (its excuse ends), then it settles too.
	await tick(20);
	pending[0]();
	await tick();
	// The second reopen still holds its own excuse while its suspend lands.
	ctx.setState('suspended');
	assert.deepEqual(alarms, [], "the second reopen's suspend is still excused");
	pending[1]();
	assert.equal(await second, null);
	detach();
});

test('a genuine pause while a late reopen suspend is pending is never taken for that suspend landing (Sol P1 on #6835)', async () => {
	const { mod, ctx, alarms, detach } = await watchedRoom();
	let settle;
	ctx.suspend = () => new Promise((resolve) => (settle = resolve));
	const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
	const timesOut = (operation, promise) =>
		operation === 'master reopen suspend' ? Promise.reject(new Error('suspend timed out')) : promise;
	assert.match(await mod.reopenRoomOutput(ctx, timesOut, 10), /timed out/);
	await tick(25); // the grace has ended
	// The room is paused by something else; our suspend settles a while later
	// against a room that is already silent.
	ctx.setState('suspended');
	await tick(5);
	assert.ok(alarms.includes('pause:suspended'), 'the genuine pause alarms');
	settle();
	await tick(5);
	// That settle landed nothing: the pause is not marked as ours, so the next
	// reopen leaves it alone, and it excuses no later edge.
	ctx.calls.length = 0;
	assert.equal(await mod.reopenRoomOutput(ctx, (_operation, promise) => promise), null);
	assert.deepEqual(ctx.calls, [], 'a pause that is not ours is never resumed by a reopen');
	alarms.length = 0;
	ctx.setState('running');
	ctx.setState('suspended');
	await tick(5);
	assert.ok(alarms.includes('pause:suspended'), 'the next pause still alarms');
	detach();
});

test('a late suspend landing never excuses a later pause while another late suspend is still pending', async () => {
	const { mod, ctx, alarms, detach } = await watchedRoom();
	const pending = [];
	ctx.suspend = () => new Promise((resolve) => pending.push(resolve));
	const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
	const timesOut = (operation, promise) =>
		operation === 'master reopen suspend' ? Promise.reject(new Error('suspend timed out')) : promise;
	assert.match(await mod.reopenRoomOutput(ctx, timesOut, 10), /timed out/);
	assert.match(await mod.reopenRoomOutput(ctx, timesOut, 10), /timed out/);
	assert.equal(pending.length, 2, 'precondition: both suspends are still pending');
	await tick(25); // both graces have ended
	ctx.setState('suspended');
	pending[0]();
	await tick(5);
	assert.deepEqual(alarms, [], 'the first late suspend landing is ours');
	ctx.setState('running');
	await tick(5);
	// The second is still pending, but this pause is not it landing.
	ctx.setState('suspended');
	await tick(5);
	assert.ok(alarms.includes('pause:suspended'), 'a landing excuses only its own edge');
	detach();
});

test('control: a timed-out reopen suspend that later fails gives the excuse back', async () => {
	const { mod, ctx, alarms, detach } = await watchedRoom();
	let fail;
	ctx.suspend = () => new Promise((_resolve, reject) => (fail = () => reject(new Error('suspend refused'))));
	const timesOut = (operation, promise) =>
		operation === 'master reopen suspend' ? Promise.reject(new Error('suspend timed out')) : promise;
	assert.match(await mod.reopenRoomOutput(ctx, timesOut), /timed out/);
	fail();
	await new Promise((r) => setTimeout(r, 0));
	ctx.setState('suspended');
	assert.ok(alarms.includes('pause:suspended'), 'a pause after our suspend failed is not excused');
	detach();
});

test('control: a reopen suspend that fails after silencing the room is still resumed by the next reopen', async () => {
	const reopen = await reopenModule();
	const ctx = roomContext();
	ctx.suspend = async () => {
		ctx.calls.push('suspend');
		ctx.setState('suspended');
		throw new Error('suspend reported a failure after it landed');
	};
	assert.match(await reopen.reopenRoomOutput(ctx, noTimeout), /after it landed/);
	assert.equal(ctx.state, 'suspended');
	ctx.calls.length = 0;
	assert.equal(await reopen.reopenRoomOutput(ctx, noTimeout), null);
	assert.deepEqual(ctx.calls, ['resume'], 'a silence this module made is undone');
});

test("an overlapping reopen whose suspend lands after another's resume keeps the room marked as ours", async () => {
	const reopen = await reopenModule();
	const ctx = roomContext();
	const pending = { suspend: [], resume: [] };
	ctx.suspend = () => new Promise((resolve, reject) => pending.suspend.push({ resolve, reject }));
	ctx.resume = () => new Promise((resolve, reject) => pending.resume.push({ resolve, reject }));
	const tick = () => new Promise((r) => setTimeout(r, 0));
	const first = reopen.reopenRoomOutput(ctx, noTimeout);
	const second = reopen.reopenRoomOutput(ctx, noTimeout);
	await tick();
	assert.equal(pending.suspend.length, 2, 'precondition: both reopens suspend the running room');
	ctx.setState('suspended');
	pending.suspend[0].resolve();
	await tick();
	ctx.setState('running');
	pending.resume[0].resolve();
	assert.equal(await first, null);
	// The second suspend lands only now, then its resume fails.
	ctx.setState('suspended');
	pending.suspend[1].resolve();
	await tick();
	pending.resume[1].reject(new Error('resume died'));
	assert.match(await second, /resume died/);
	assert.equal(ctx.state, 'suspended');
	ctx.resume = async () => {
		ctx.calls.push('resume');
		ctx.setState('running');
	};
	ctx.calls.length = 0;
	assert.equal(await reopen.reopenRoomOutput(ctx, noTimeout), null);
	assert.deepEqual(ctx.calls, ['resume'], 'the next reopen undoes the silence the second reopen left');
	assert.equal(ctx.state, 'running');
});

/** The shell's housekeeping put the macOS default back on the pinned MAIN. */
function shellReassertsMain() {
	ReplyWorker.last.onmessage({ data: { kind: 'message', payload: { type: 'master_reasserted', from_uid: SPEAKERS } } });
}

test('a shell MAIN reassert reopens the room, counted as MAIN switching (Codex P1 BLOCKING on #6835)', async () => {
	const ctx = roomContext();
	await audio.selectMasterOutput(USB, () => ({ context: ctx, masterGain: ctx.createGain() }));
	ctx.calls.length = 0;
	let finishResume;
	const resume = ctx.resume;
	ctx.resume = () => new Promise((resolve) => (finishResume = () => resume().then(resolve)));
	shellReassertsMain();
	await waitFor(() => finishResume !== undefined, 'the reassert to reopen the room');
	assert.equal(audio.outputSwitchesPending().master, true, 'no Test beeps through the reopen');
	finishResume();
	await waitFor(() => !audio.outputSwitchesPending().master, 'the reopen to settle');
	assert.deepEqual(ctx.calls, ['suspend', 'resume']);
	assert.equal(audio.mixerState.headphones.error, null);
	assert.equal(audio.mainReopenFailed(), false);

	// A failed reopen shows the retry error and refuses the Test.
	ctx.resume = async () => {
		throw new Error('resume died');
	};
	shellReassertsMain();
	await waitFor(() => audio.mainReopenFailed(), 'the failed reopen to be published');
	assert.match(audio.mixerState.headphones.error ?? '', /resume died.*Pick the device again/);
	assert.equal(audio.mixerState.headphones.routes.master.state, 'selected');
	ctx.resume = resume;

	// A later reassert that reopens cleanly clears that retry error, and
	// leaves an unrelated error alone (control).
	shellReassertsMain();
	await waitFor(() => !audio.mainReopenFailed(), 'the clean reopen to land');
	assert.equal(audio.mixerState.headphones.error, null, 'the recovered room no longer says pick again');
	audio.mixerState.headphones.error = 'CUE output did not open';
	shellReassertsMain();
	await waitFor(() => !audio.outputSwitchesPending().master, 'the reopen to settle');
	assert.equal(audio.mixerState.headphones.error, 'CUE output did not open');
});

/** The shell's housekeeping dropped the MAIN pin macOS kept overriding. */
function shellReleasesMainPin() {
	ReplyWorker.last.onmessage({
		data: { kind: 'message', payload: { type: 'master_pin_released', reason: 'overridden', message: 'MAIN now follows the system output' } }
	});
}

test('a released shell MAIN pin reopens the room, and a failed reopen refuses Tests (Codex P1 BLOCKING on #6835)', async () => {
	const ctx = roomContext();
	await audio.selectMasterOutput(USB, () => ({ context: ctx, masterGain: ctx.createGain() }));
	ctx.calls.length = 0;
	shellReleasesMainPin();
	await waitFor(() => ctx.calls.includes('resume'), 'the release to reopen the room');
	await waitFor(() => !audio.outputSwitchesPending().master, 'the reopen to settle');
	assert.deepEqual(ctx.calls.filter((call) => call === 'suspend' || call === 'resume'), ['suspend', 'resume']);
	assert.equal(audio.mixerState.headphones.selected_master_output_device_id, null, 'MAIN follows the system output');
	assert.equal(audio.mainReopenFailed(), false, 'a clean reopen publishes no retry error');

	// A reopen that fails is published, so MAIN and AUDIO IN Tests refuse.
	await audio.selectMasterOutput(USB, () => ({ context: ctx, masterGain: ctx.createGain() }));
	const resume = ctx.resume;
	ctx.resume = async () => {
		throw new Error('resume died');
	};
	shellReleasesMainPin();
	await waitFor(() => audio.mainReopenFailed(), 'the failed reopen to be published');
	assert.match(audio.mixerState.headphones.error ?? '', /resume died.*Pick the device again/);
	ctx.resume = resume;
});

test("a retired route's reopen landing clean never clears the replacement route's failed reopen (Codex P1 BLOCKING on #6835)", async () => {
	const old = roomContext();
	await audio.selectMasterOutput(USB, () => ({ context: old, masterGain: old.createGain() }));
	const oldResume = old.resume;
	let finishOld;
	old.resume = () => new Promise((resolve) => (finishOld = () => oldResume().then(resolve)));
	shellReassertsMain();
	await waitFor(() => finishOld !== undefined, 'the old reassert to reopen its room');
	// /performance tears down and mounts a replacement room, whose reopen fails.
	audio.disposeHeadphoneMonitor();
	const ctx = roomContext();
	await audio.selectMasterOutput(USB, () => ({ context: ctx, masterGain: ctx.createGain() }));
	const resume = ctx.resume;
	ctx.resume = async () => {
		throw new Error('resume died');
	};
	shellReassertsMain();
	await waitFor(() => audio.mainReopenFailed(), "the replacement's failed reopen to be published");
	ctx.resume = resume;
	// The retired room's reopen lands clean only now.
	finishOld();
	await new Promise((r) => setTimeout(r, 5));
	assert.equal(audio.mainReopenFailed(), true, 'the replacement room still refuses its Tests');
	assert.match(audio.mixerState.headphones.error ?? '', /resume died.*Pick the device again/);
});

test('a shell MAIN pin release retires a MAIN pick still reopening, so it never republishes the released device (Codex P1 BLOCKING on #6835)', async () => {
	const ctx = roomContext();
	const source = () => ({ context: ctx, masterGain: ctx.createGain() });
	await audio.selectMasterOutput(SPEAKERS, source);
	const resume = ctx.resume;
	let finishPick;
	ctx.resume = () => new Promise((resolve) => (finishPick = () => resume().then(resolve)));
	const pick = audio.selectMasterOutput(USB, source).then(() => 'resolved', (error) => error);
	await waitFor(() => finishPick !== undefined, "the pick's reopen to start");
	ctx.resume = resume;
	shellReleasesMainPin();
	finishPick();
	await pick;
	await waitFor(() => !audio.outputSwitchesPending().master, 'the reopens to settle');
	assert.equal(audio.mixerState.headphones.selected_master_output_device_id, null, 'the released MAIN is not republished');
	assert.equal(audio.landedMasterSinkId(), null, 'nothing is known landed once the system owns MAIN');
});

test("control: a MAIN pick made during a shell reassert's reopen keeps its clean result", async () => {
	const ctx = roomContext();
	const source = () => ({ context: ctx, masterGain: ctx.createGain() });
	await audio.selectMasterOutput(USB, source);
	const resume = ctx.resume;
	let failResume;
	ctx.resume = () => new Promise((_, reject) => (failResume = () => reject(new Error('resume died'))));
	shellReassertsMain();
	await waitFor(() => failResume !== undefined, 'the reassert to reopen the room');
	ctx.resume = resume;
	await audio.selectMasterOutput(SPEAKERS, source);
	failResume();
	await waitFor(() => !audio.outputSwitchesPending().master, 'the reopen to settle');
	assert.equal(audio.mixerState.headphones.error, null, "the stale reassert's failure is not shown over the pick");
	assert.equal(audio.mainReopenFailed(), false);
});
