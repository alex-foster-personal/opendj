import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { mock, test } from 'node:test';

import {
	IO_TEST_BEEPS,
	IO_TEST_METER_FILL,
	ioMeterFill,
	ioTestDeviceId,
	ioTestDurationSec,
	ioTestEnvelope,
	ioTestRefusal,
	meterFillFromRms,
	rmsOf
} from '../../src/lib/player/io-device-test.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';

// The Web Audio half imports headphones.ts, so it is bundled like the other
// headphone tests rather than loaded as plain stripped TypeScript.
const { INPUT_METER_OPEN_TIMEOUT_MS, liveBeepNode, outputMoveCutsTest, liveCueSinkOpen, liveRouteRefusal, outputSwitchRefusal, playIoTestBeeps, startInputLevelWatch, stopIoTestBeeps } = await loadTypeScriptModule('src/lib/player/io-device-test-audio.ts');

test('the device test is three short beeps under a second', () => {
	assert.equal(IO_TEST_BEEPS.length, 3);
	assert.ok(IO_TEST_BEEPS.every((beep) => beep.hz === 880 && beep.holdSec > 0));
	const duration = ioTestDurationSec();
	assert.ok(duration > 0.3 && duration < 1, `duration ${duration}`);
});

test('the meter envelope is up during each beep and down in the gaps', () => {
	assert.equal(ioTestEnvelope(-0.1), 0);
	assert.equal(ioTestEnvelope(Number.NaN), 0);
	let t = 0;
	for (const beep of IO_TEST_BEEPS) {
		assert.equal(ioTestEnvelope(t + beep.holdSec / 2), IO_TEST_METER_FILL);
		t += beep.holdSec;
		if (beep.gapSec > 0) assert.equal(ioTestEnvelope(t + beep.gapSec / 2), 0);
		t += beep.gapSec;
	}
	assert.equal(ioTestEnvelope(t + 0.05), 0);
});

test('rms maps from silence to full scale across -48 dBFS', () => {
	assert.equal(meterFillFromRms(null), 0);
	assert.equal(meterFillFromRms(0), 0);
	assert.equal(meterFillFromRms(Number.NaN), 0);
	assert.equal(meterFillFromRms(1), 1);
	assert.equal(meterFillFromRms(4), 1);
	const half = meterFillFromRms(10 ** (-24 / 20));
	assert.ok(Math.abs(half - 0.5) < 0.02, `half-scale fill ${half}`);
});

test('the drawn meter keeps whichever of the bus and the beep is taller', () => {
	assert.equal(ioMeterFill(0, 0), 0);
	assert.equal(ioMeterFill(1, 0), 1);
	assert.equal(ioMeterFill(0, IO_TEST_METER_FILL), IO_TEST_METER_FILL);
	assert.equal(ioMeterFill(1, IO_TEST_METER_FILL), 1);
	assert.equal(ioMeterFill(0, Number.NaN), 0);
});

test('rmsOf ignores empty and non-finite samples', () => {
	assert.equal(rmsOf([]), 0);
	assert.equal(rmsOf([Number.NaN]), 0);
	assert.ok(Math.abs(rmsOf([0.3, -0.3]) - 0.3) < 1e-9);
});

function withFakeMic(AudioContextImpl, run) {
	const stopped = [];
	const stream = { getTracks: () => [{ stop: () => stopped.push('track') }] };
	const savedNav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
	const savedCtx = globalThis.AudioContext;
	Object.defineProperty(globalThis, 'navigator', {
		configurable: true,
		value: { mediaDevices: { getUserMedia: async () => stream } }
	});
	globalThis.AudioContext = AudioContextImpl;
	return run(stopped).finally(() => {
		if (savedNav) Object.defineProperty(globalThis, 'navigator', savedNav);
		else delete globalThis.navigator;
		globalThis.AudioContext = savedCtx;
	});
}

test('the input meter never leaves the mic open when its audio graph fails to build', async () => {
	let closed = 0;
	class BrokenSourceContext {
		state = 'running';
		createMediaStreamSource() {
			throw new Error('no source');
		}
		async close() {
			closed += 1;
			this.state = 'closed';
		}
	}
	await withFakeMic(BrokenSourceContext, async (stopped) => {
		await assert.rejects(startInputLevelWatch('mic', () => {}), /no source/);
		assert.deepEqual(stopped, ['track']);
		assert.equal(closed, 1);
	});
});

test('the input meter rejects where microphones cannot be opened, so an AUDIO IN Test never passes unmeasured', async () => {
	const savedNav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
	Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: {} } });
	try {
		await assert.rejects(startInputLevelWatch('mic', () => {}), /cannot open microphones/);
	} finally {
		if (savedNav) Object.defineProperty(globalThis, 'navigator', savedNav);
		else delete globalThis.navigator;
	}
});

test('an input meter whose context resume never settles times out and releases the mic', async () => {
	let closed = 0;
	class StalledContext {
		state = 'suspended';
		resume() {
			return new Promise(() => {});
		}
		async close() {
			closed += 1;
			this.state = 'closed';
		}
	}
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		await withFakeMic(StalledContext, async (stopped) => {
			const settled = assert.rejects(startInputLevelWatch('mic', () => {}), /timed out/);
			for (let i = 0; i < 5; i += 1) await Promise.resolve();
			mock.timers.tick(5_000);
			await settled;
			assert.deepEqual(stopped, ['track'], 'the mic must not stay open behind a stalled resume');
			assert.equal(closed, 1);
		});
	} finally {
		mock.timers.reset();
	}
});

test('control: a working input meter keeps the mic open until it is stopped', async () => {
	let closed = 0;
	class WorkingContext {
		state = 'running';
		createMediaStreamSource() {
			return { connect() {}, disconnect() {} };
		}
		createAnalyser() {
			return { fftSize: 0, getFloatTimeDomainData() {} };
		}
		async close() {
			closed += 1;
			this.state = 'closed';
		}
	}
	const savedRaf = globalThis.requestAnimationFrame;
	const savedCancel = globalThis.cancelAnimationFrame;
	globalThis.requestAnimationFrame = () => 1;
	globalThis.cancelAnimationFrame = () => {};
	try {
		await withFakeMic(WorkingContext, async (stopped) => {
			const stop = await startInputLevelWatch('mic', () => {});
			assert.deepEqual(stopped, []);
			assert.equal(closed, 0);
			stop();
			assert.deepEqual(stopped, ['track']);
			assert.equal(closed, 1);
		});
	} finally {
		globalThis.requestAnimationFrame = savedRaf;
		globalThis.cancelAnimationFrame = savedCancel;
	}
});

// [if] an open input watch's microphone ends [then] its graph is torn down at
// once (source disconnected, context closed, tracks stopped), and the caller's
// later stop tears down nothing twice
test('an input watch whose microphone ends releases its graph without waiting for the stop (Sol P2 on #6835)', async () => {
	const log = [];
	class Ctx {
		state = 'running';
		createMediaStreamSource() {
			return { connect() {}, disconnect: () => log.push('disconnect') };
		}
		createAnalyser() {
			return { fftSize: 4, getFloatTimeDomainData: (buf) => buf.fill(0.5) };
		}
		async close() {
			log.push('close');
		}
	}
	const savedRaf = globalThis.requestAnimationFrame;
	const savedCancel = globalThis.cancelAnimationFrame;
	const savedNav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
	const savedCtx = globalThis.AudioContext;
	let frame = null;
	globalThis.requestAnimationFrame = (fn) => ((frame = fn), 1);
	globalThis.cancelAnimationFrame = () => (frame = null);
	globalThis.AudioContext = Ctx;
	const open = (tracks) =>
		Object.defineProperty(globalThis, 'navigator', {
			configurable: true,
			value: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => tracks }) } }
		});
	try {
		const dead = { readyState: 'live', stop: () => log.push('stop dead') };
		const other = { readyState: 'live', stop: () => log.push('stop other') };
		open([dead, other]);
		const stop = await startInputLevelWatch('mic', () => {}, undefined, () => {});
		frame();
		dead.readyState = 'ended';
		dead.onended();
		await new Promise((r) => setTimeout(r, 0));
		assert.deepEqual(log, ['disconnect', 'close', 'stop dead', 'stop other'], 'the ended microphone releases everything at once');
		stop();
		await new Promise((r) => setTimeout(r, 0));
		assert.deepEqual(log, ['disconnect', 'close', 'stop dead', 'stop other'], 'the later stop releases nothing twice');
		// Control: a live microphone keeps its graph until its caller stops it.
		log.length = 0;
		const live = { readyState: 'live', stop: () => log.push('stop live') };
		open([live]);
		const stopLive = await startInputLevelWatch('mic', () => {}, undefined, () => {});
		frame();
		await new Promise((r) => setTimeout(r, 0));
		assert.deepEqual(log, [], 'a live microphone is not released early');
		stopLive();
		await new Promise((r) => setTimeout(r, 0));
		assert.deepEqual(log, ['disconnect', 'close', 'stop live']);
	} finally {
		globalThis.requestAnimationFrame = savedRaf;
		globalThis.cancelAnimationFrame = savedCancel;
		if (savedNav) Object.defineProperty(globalThis, 'navigator', savedNav);
		else delete globalThis.navigator;
		globalThis.AudioContext = savedCtx;
	}
});

// [if] an open input watch's track ends (fired, or read as ended on a frame)
// [then] the watch stops reading and calls onEnded once, even when it is only
// seen at the stop; a plain stop of a live microphone calls nothing and zeroes the meter
test('an input watch whose microphone ends stops and reports it, once (Codex P2 BLOCKING on #6835)', async () => {
	class Ctx {
		state = 'running';
		createMediaStreamSource() {
			return { connect() {}, disconnect() {} };
		}
		createAnalyser() {
			return { fftSize: 4, getFloatTimeDomainData: (buf) => buf.fill(0.5) };
		}
		async close() {}
	}
	const savedRaf = globalThis.requestAnimationFrame;
	const savedCancel = globalThis.cancelAnimationFrame;
	const savedNav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
	const savedCtx = globalThis.AudioContext;
	let frame = null;
	globalThis.requestAnimationFrame = (fn) => ((frame = fn), 1);
	globalThis.cancelAnimationFrame = () => (frame = null);
	globalThis.AudioContext = Ctx;
	const open = (track) =>
		Object.defineProperty(globalThis, 'navigator', {
			configurable: true,
			value: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [track] }) } }
		});
	try {
		for (const how of ['event', 'frame']) {
			const track = { readyState: 'live', stop() {} };
			open(track);
			const levels = [];
			const ended = [];
			const stop = await startInputLevelWatch('mic', (rms) => levels.push(rms), undefined, (reason) => ended.push(reason));
			frame();
			assert.equal(levels.at(-1), 0.5);
			track.readyState = 'ended';
			if (how === 'event') track.onended();
			else frame();
			assert.equal(ended.length, 1, `${how}: onEnded called`);
			assert.match(ended[0], /stopped sending audio/);
			assert.equal(levels.at(-1), 0, `${how}: the meter drops to zero`);
			assert.equal(frame, null, `${how}: no frame reads the dead device`);
			track.onended?.();
			stop();
			assert.equal(ended.length, 1, `${how}: reported once`);
			assert.deepEqual(levels, [0.5, 0], `${how}: the stop after an end writes no second zero`);
		}
		// Ended after the last frame, its event not yet delivered, then stopped:
		// still reported, at the stop (Codex P2 BLOCKING on #6835).
		const unseen = { readyState: 'live', stop() {} };
		open(unseen);
		const late = [];
		const stopLate = await startInputLevelWatch('mic', () => {}, undefined, (reason) => late.push(reason));
		frame();
		unseen.readyState = 'ended';
		stopLate();
		unseen.onended?.();
		assert.equal(late.length, 1, 'a capture that ended unseen is reported at the stop, once');
		// Control: a live microphone stopped by its caller reports nothing, and
		// its meter drops to zero rather than holding the last level (Sol P1 on
		// #6835, Fri 30 Oct 2026).
		const live = { readyState: 'live', stop() {} };
		open(live);
		const ended = [];
		const liveLevels = [];
		const stop = await startInputLevelWatch('mic', (rms) => liveLevels.push(rms), undefined, (reason) => ended.push(reason));
		frame();
		assert.equal(liveLevels.at(-1), 0.5);
		stop();
		live.onended?.();
		assert.deepEqual(ended, []);
		assert.deepEqual(liveLevels, [0.5, 0], 'a stop zeroes the meter once and reads nothing after');
	} finally {
		globalThis.requestAnimationFrame = savedRaf;
		globalThis.cancelAnimationFrame = savedCancel;
		if (savedNav) Object.defineProperty(globalThis, 'navigator', savedNav);
		else delete globalThis.navigator;
		globalThis.AudioContext = savedCtx;
	}
});

test('a CUE test with no CUE device picked is refused before any audio opens, and a picked one is not', async () => {
	// One Output clears the CUE pick but can leave the live bridge connected, so
	// the refusal has to come from the pick, not from the graph.
	let opened = 0;
	const savedCtx = globalThis.AudioContext;
	globalThis.AudioContext = class {
		constructor() {
			opened += 1;
			throw new Error('an AudioContext was opened');
		}
	};
	try {
		for (const none of [null, '']) {
			await assert.rejects(playIoTestBeeps('cue', none), /No headphone output is picked/);
		}
		assert.equal(opened, 0);
		// Control: a picked CUE device gets past the pick check to the next one.
		await assert.rejects(playIoTestBeeps('cue', 'usb-headphones'), /not open yet/);
	} finally {
		globalThis.AudioContext = savedCtx;
	}
});

test('an AUDIO IN or MAIN test before the graph is built beeps on the picked MAIN, and the system row on the empty id', async () => {
	// AUDIO IN beeps on MAIN, so its test gets MAIN's device too, never the OS default.
	const sinks = [];
	const savedCtx = globalThis.AudioContext;
	globalThis.AudioContext = class {
		constructor() {
			this.state = 'running';
		}
		async setSinkId(id) {
			sinks.push(id);
			throw new Error('stop after setSinkId');
		}
		async close() {
			this.state = 'closed';
		}
	};
	try {
		await assert.rejects(playIoTestBeeps('input', 'usb-main'), /stop after setSinkId/);
		await assert.rejects(playIoTestBeeps('master', 'usb-main'), /stop after setSinkId/);
		await assert.rejects(playIoTestBeeps('input', 'default'), /stop after setSinkId/);
		assert.deepEqual(sinks, ['usb-main', 'usb-main', '']);
	} finally {
		globalThis.AudioContext = savedCtx;
	}
});

test('in split L/R a MAIN or AUDIO IN Test beeps only the left half, and CUE only cueSum', () => {
	const ctx = { destination: 'destination' };
	const nodes = { bridgeSender: {}, bridgeInput: 'bridgeInput', cueSum: 'cueSum', splitLeftGain: 'splitLeftGain' };
	assert.equal(liveBeepNode('master', ctx, nodes, 'split_cable'), 'splitLeftGain');
	assert.equal(liveBeepNode('input', ctx, nodes, 'split_cable'), 'splitLeftGain');
	assert.equal(liveBeepNode('cue', ctx, nodes, 'split_cable'), 'cueSum');
	// Control: outside split MAIN uses the destination and CUE the connected bridge.
	assert.equal(liveBeepNode('master', ctx, nodes, 'two_outputs'), 'destination');
	assert.equal(liveBeepNode('input', ctx, nodes, 'practice'), 'destination');
	assert.equal(liveBeepNode('cue', ctx, nodes, 'two_outputs'), 'bridgeInput');
	assert.equal(liveBeepNode('cue', ctx, { ...nodes, bridgeSender: null }, 'two_outputs'), 'cueSum');
});

test('a Test whose setSinkId or resume never settles fails with a timeout and closes its context', async () => {
	let closed = 0;
	const savedCtx = globalThis.AudioContext;
	const make = (state, sink) =>
		class {
			state = state;
			setSinkId() {
				return sink;
			}
			resume() {
				return new Promise(() => {});
			}
			async close() {
				closed += 1;
				this.state = 'closed';
			}
		};
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		for (const Ctx of [make('running', new Promise(() => {})), make('suspended', Promise.resolve())]) {
			globalThis.AudioContext = Ctx;
			const test = playIoTestBeeps('master', 'usb-main');
			const settled = assert.rejects(test, /timed out/);
			for (let i = 0; i < 5; i += 1) await Promise.resolve();
			mock.timers.tick(5_000);
			await settled;
		}
		assert.equal(closed, 2);
	} finally {
		mock.timers.reset();
		globalThis.AudioContext = savedCtx;
	}
});

test('an aborted microphone open whose meter context is still resuming ends at once, stops the mic and closes the context', async () => {
	const stopped = [];
	let closed = 0;
	let closeAsked = 0;
	const savedNav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
	const savedCtx = globalThis.AudioContext;
	Object.defineProperty(globalThis, 'navigator', {
		configurable: true,
		value: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop: () => stopped.push('track') }] }) } }
	});
	globalThis.AudioContext = class {
		state = 'suspended';
		resume() {
			return new Promise(() => {});
		}
		close() {
			closeAsked += 1;
			// A close that is slow to settle must not hold the caller either.
			return new Promise((resolve) => setTimeout(() => ((closed += 1), (this.state = 'closed'), resolve()), 50));
		}
	};
	try {
		const abort = new AbortController();
		let failed = false;
		const watch = startInputLevelWatch('mic', () => {}, abort.signal);
		watch.catch(() => (failed = true));
		for (let i = 0; i < 10; i += 1) await Promise.resolve();
		// Control: unaborted, the meter is still waiting on its context.
		assert.equal(failed, false);
		assert.deepEqual(stopped, []);
		abort.abort(new Error('The performance view closed during the Test.'));
		for (let i = 0; i < 10 && !failed; i += 1) await Promise.resolve();
		assert.equal(failed, true, 'the abort ends the open without waiting out the resume or the close');
		await assert.rejects(watch, /performance view closed/);
		assert.deepEqual(stopped, ['track'], 'the microphone stops at once');
		assert.equal(closeAsked, 1, 'the meter context is closed');
		await new Promise((r) => setTimeout(r, 80));
		assert.equal(closed, 1);
	} finally {
		globalThis.AudioContext = savedCtx;
		if (savedNav) Object.defineProperty(globalThis, 'navigator', savedNav);
		else delete globalThis.navigator;
	}
});

test('aborting a sounding Test\'s signal cuts that Test\'s beep, never a later one', async () => {
	const savedCtx = globalThis.AudioContext;
	const param = () => ({ value: 0, cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime() {}, setTargetAtTime() {} });
	let stops = 0;
	// The scheduled stop never ends the tone here; only a cut (a second stop) does.
	globalThis.AudioContext = class {
		state = 'running';
		currentTime = 0;
		destination = {};
		createOscillator() {
			let calls = 0;
			const osc = {
				frequency: param(),
				connect() {},
				disconnect() {},
				start() {},
				stop() {
					calls += 1;
					stops += 1;
					if (calls > 1) queueMicrotask(() => osc.onended?.());
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
	const drain = async (until) => {
		for (let i = 0; i < 20 && !until(); i += 1) await Promise.resolve();
	};
	try {
		const abort = new AbortController();
		let done = false;
		let completed;
		const test = playIoTestBeeps('master', null, () => {}, abort.signal).then((played) => ((completed = played), (done = true)));
		await drain(() => stops > 0);
		await drain(() => false);
		// Control: unaborted, the tone keeps sounding.
		assert.equal(done, false);
		abort.abort();
		await drain(() => done);
		assert.equal(done, true, 'the abort cut the beep it owned');
		await test;
		assert.equal(completed, false, 'a cut Test reports that its beeps did not all play');
		assert.equal(await playIoTestBeeps('master', null, () => {}, AbortSignal.abort()), false, 'an aborted caller reports nothing played');
		// A signal aborted after a later Test began leaves that Test sounding.
		const stale = new AbortController();
		const first = playIoTestBeeps('master', null, () => {}, stale.signal);
		await drain(() => false);
		let laterDone = false;
		const later = playIoTestBeeps('master', null).then(() => (laterDone = true));
		// Aborted while the cut first Test is still fading out, so its
		// listener is still attached when the later Test already owns the tone.
		stale.abort();
		await first;
		await new Promise((r) => setTimeout(r, 30));
		assert.equal(laterDone, false, 'an old Test\'s abort must not cut the next one');
		stopIoTestBeeps();
		await later;
		// Already aborted: nothing plays.
		const before = stops;
		await playIoTestBeeps('master', null, () => {}, AbortSignal.abort());
		assert.equal(stops, before);
	} finally {
		globalThis.AudioContext = savedCtx;
	}
});

test('a slow resume never ends a Test before its tones play, and a stalled output reports the pattern incomplete', async () => {
	const savedCtx = globalThis.AudioContext;
	const param = () => ({ value: 0, cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime() {}, setTargetAtTime() {} });
	let resumeLater;
	let endsNaturally = true;
	let scheduled = 0;
	const make = (state) =>
		class {
			state = state;
			currentTime = 0;
			destination = {};
			resume() {
				return new Promise((resolve) => (resumeLater = () => ((this.state = 'running'), resolve())));
			}
			createOscillator() {
				const osc = {
					frequency: param(),
					connect() {},
					disconnect() {},
					start() {
						scheduled += 1;
					},
					stop() {
						// The tone ends on the audio clock, after the pattern plays.
						if (endsNaturally) setTimeout(() => osc.onended?.(), 500);
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
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		// The temp context opens running; it is suspended again by the time the
		// beeps are scheduled, and that resume takes 3 s (past the pattern's length).
		globalThis.AudioContext = class extends make('running') {
			createOscillator() {
				this.state = 'suspended';
				return super.createOscillator();
			}
		};
		let result;
		const slow = playIoTestBeeps('master', null).then((played) => (result = played));
		for (let i = 0; i < 10 && resumeLater === undefined; i += 1) await Promise.resolve();
		mock.timers.tick(3_000);
		for (let i = 0; i < 10; i += 1) await Promise.resolve();
		assert.equal(result, undefined, 'the Test has not ended while its device resumes');
		resumeLater();
		for (let i = 0; i < 10; i += 1) await Promise.resolve();
		assert.equal(result, undefined, 'the Test waits for its tones to play out');
		mock.timers.tick(500);
		await slow;
		assert.equal(scheduled, 1);
		assert.equal(result, true, 'tones that played out after a slow resume count as played');
		// A stalled output (the tone never ends) finishes on the timer, as not played.
		endsNaturally = false;
		globalThis.AudioContext = make('running');
		const stalled = playIoTestBeeps('master', null);
		for (let i = 0; i < 10; i += 1) await Promise.resolve();
		mock.timers.tick(10_000);
		assert.equal(await stalled, false, 'a Test ended by its fallback did not play');
	} finally {
		mock.timers.reset();
		globalThis.AudioContext = savedCtx;
	}
});

test('a Test cut while its output is still opening ends at once and closes the context it opened', async () => {
	let closed = 0;
	const savedCtx = globalThis.AudioContext;
	const make = (state, sink) =>
		class {
			state = state;
			setSinkId() {
				return sink;
			}
			resume() {
				return new Promise(() => {});
			}
			async close() {
				closed += 1;
				this.state = 'closed';
			}
		};
	try {
		for (const Ctx of [make('running', new Promise(() => {})), make('suspended', Promise.resolve())]) {
			globalThis.AudioContext = Ctx;
			let done = false;
			const test = playIoTestBeeps('master', 'usb-main').then(() => (done = true));
			for (let i = 0; i < 5; i += 1) await Promise.resolve();
			// Control: uncut, the Test is still waiting on its device.
			assert.equal(done, false);
			stopIoTestBeeps();
			for (let i = 0; i < 10 && !done; i += 1) await Promise.resolve();
			assert.equal(done, true, 'a cut never waits out the output open');
			await test;
		}
		assert.equal(closed, 2, 'each cut Test closes the context it opened');
		// A new Test cuts one still opening the same way.
		globalThis.AudioContext = make('running', new Promise(() => {}));
		let first = false;
		const one = playIoTestBeeps('master', 'usb-main').then(() => (first = true));
		for (let i = 0; i < 5; i += 1) await Promise.resolve();
		const two = playIoTestBeeps('master', 'usb-main');
		for (let i = 0; i < 10 && !first; i += 1) await Promise.resolve();
		assert.equal(first, true);
		await one;
		stopIoTestBeeps();
		await two;
		// The context opened, then sits suspended again when the beeps are
		// scheduled (the live graph's case): that resume is raced too.
		let resumes = 0;
		globalThis.AudioContext = class extends make('suspended', Promise.resolve()) {
			resume() {
				resumes += 1;
				return resumes === 1 ? Promise.resolve() : new Promise(() => {});
			}
			createOscillator() {
				return { frequency: { value: 0 }, connect() {}, disconnect() {}, start() {}, stop() {}, onended: null };
			}
			createGain() {
				const param = { value: 0, cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime() {}, setTargetAtTime() {} };
				return { gain: param, connect() {}, disconnect() {} };
			}
		};
		let playDone = false;
		const playing = playIoTestBeeps('master', 'usb-main').then(() => (playDone = true));
		for (let i = 0; i < 10 && resumes < 2; i += 1) await Promise.resolve();
		assert.equal(resumes, 2, 'the Test reached the second resume');
		for (let i = 0; i < 5; i += 1) await Promise.resolve();
		assert.equal(playDone, false);
		stopIoTestBeeps();
		for (let i = 0; i < 10 && !playDone; i += 1) await Promise.resolve();
		assert.equal(playDone, true, 'a cut never waits out the live resume');
		await playing;
	} finally {
		globalThis.AudioContext = savedCtx;
	}
});

test('a CUE Test in split L/R plays on MAIN\'s device, and outside split needs its own CUE pick', () => {
	const hp = (output_mode, cue, main) => ({
		output_mode,
		selected_output_device_id: cue,
		selected_master_output_device_id: main
	});
	// Split L/R (a splitter cable or the same-device split) clears the CUE pick.
	assert.equal(ioTestDeviceId('cue', hp('split_cable', null, 'usb-main')), 'usb-main');
	assert.equal(ioTestDeviceId('cue', hp('split_cable', null, null)), 'default');
	// Control: outside split a cleared CUE pick stays null, so the test is refused.
	assert.equal(ioTestDeviceId('cue', hp('two_outputs', null, 'usb-main')), null);
	assert.equal(ioTestDeviceId('cue', hp('two_outputs', 'phones', 'usb-main')), 'phones');
	assert.equal(ioTestDeviceId('input', hp('two_outputs', 'phones', 'usb-main')), 'usb-main');
	assert.equal(ioTestDeviceId('master', hp('split_cable', null, 'usb-main')), 'usb-main');
});

test('an AUDIO IN Test opens the microphone inside the owned run; the passive meter opens only with access granted', () => {
	const source = readFileSync(new URL('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte', import.meta.url), 'utf8');
	const play = source.slice(source.indexOf('async function playDeviceTest('));
	const run = play.slice(play.indexOf('await testrun('), play.indexOf('} catch (error) {'));
	assert.ok(run.indexOf('io.startInputLevelWatch(') >= 0, 'the Test opens the microphone inside testrun');
	assert.ok(run.indexOf('!ioSurface.open') >= 0 && run.indexOf('!ioSurface.open') < run.indexOf('io.startInputLevelWatch('),
		'a queued Test re-checks the panel before it opens anything');
	assert.ok(run.indexOf('stopInput()') >= 0, 'the Test closes the microphone it opened');
	// A mic prompt left up must not hold the headphone scope past the panel
	// or the route: the open takes this Test's abort, which the route's own
	// abort, a new Test, closing the panel and unmount all fire.
	assert.match(run, /local\.signal\s*[,)]/, 'the Test passes its abort to the microphone open');
	// A capture that ended unseen is reported by the stop, so the note is read after it.
	assert.ok(run.indexOf('testNote = inputEnded') > run.indexOf('stopInput();'), 'the panel reads the ended reason after stopping the microphone');
	assert.match(run, /routeSignal\.addEventListener\('abort'/, 'the route ending aborts the open');
	assert.match(run.slice(run.indexOf('io.playIoTestBeeps(')), /^io\.playIoTestBeeps\([\s\S]*?local\.signal\s*\)/, 'a new Test cuts the beep it replaces');
	const closeEffect = source.slice(source.indexOf('if (ioSurface.open) return;'), source.indexOf('// The clock only drives'));
	assert.equal(closeEffect.match(/abortTest\(\);/g)?.length, 2, 'closing the panel and unmount abort the open');
	assert.doesNotMatch(closeEffect, /stopIoTestBeeps/, 'panel cleanup cuts only its own Test, never an agent\'s');
	assert.ok(play.indexOf('abortTest();') < play.indexOf('await testrun('), 'a new Test aborts the last one');
	const passive = source.slice(source.indexOf('void inputRecheck;'), source.indexOf('function testDeviceId('));
	assert.match(passive, /const allowed = await io\.microphonePermissionGranted\(\);/);
	assert.doesNotMatch(passive, /forced/, 'only a Test may open a microphone that is not yet granted');
	assert.match(passive, /opening\.signal\s*[,)]/, 'the passive meter passes its abort to the microphone open');
	assert.match(passive, /return \(\) => \{\s*cancelled = true;\s*opening\.abort\(/, 'its cleanup aborts an open still settling');
});

test('a microphone open that never settles times out, and a grant that lands later is stopped at once', async () => {
	const stopped = [];
	let grant;
	const savedNav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
	Object.defineProperty(globalThis, 'navigator', {
		configurable: true,
		value: { mediaDevices: { getUserMedia: () => new Promise((resolve) => (grant = resolve)) } }
	});
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		const watch = startInputLevelWatch('mic', () => {});
		let failed = false;
		watch.catch(() => (failed = true));
		const settled = assert.rejects(watch, /input meter open timed out/);
		for (let i = 0; i < 5; i += 1) await Promise.resolve();
		// Control: the bound leaves time to answer a permission prompt.
		mock.timers.tick(INPUT_METER_OPEN_TIMEOUT_MS - 1);
		for (let i = 0; i < 5; i += 1) await Promise.resolve();
		assert.equal(failed, false, 'the open must not fail before its bound');
		assert.ok(INPUT_METER_OPEN_TIMEOUT_MS >= 20_000, 'a permission prompt needs time to be read and answered');
		mock.timers.tick(1);
		await settled;
		grant({ getTracks: () => [{ stop: () => stopped.push('track') }] });
		for (let i = 0; i < 5; i += 1) await Promise.resolve();
		assert.deepEqual(stopped, ['track'], 'a late grant must not leave the mic open');
	} finally {
		mock.timers.reset();
		if (savedNav) Object.defineProperty(globalThis, 'navigator', savedNav);
		else delete globalThis.navigator;
	}
});

// [if] a microphone open times out and the browser later rejects it [then]
// that real cause is recorded, not dropped behind the timeout.
test('a microphone open that times out and is later refused records the refusal', async () => {
	let refuse;
	const warned = [];
	const savedNav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
	Object.defineProperty(globalThis, 'navigator', {
		configurable: true,
		value: { mediaDevices: { getUserMedia: () => new Promise((_, reject) => (refuse = reject)) } }
	});
	const warn = mock.method(console, 'warn', (line) => warned.push(String(line)));
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		const watch = startInputLevelWatch('mic', () => {});
		const settled = assert.rejects(watch, /input meter open timed out/);
		for (let i = 0; i < 5; i += 1) await Promise.resolve();
		mock.timers.tick(INPUT_METER_OPEN_TIMEOUT_MS);
		await settled;
		assert.equal(warned.some((line) => line.includes('input-meter-open-late-failure')), false, 'control: nothing late is recorded before the refusal');
		refuse(new Error('Permission denied by system'));
		for (let i = 0; i < 5; i += 1) await Promise.resolve();
		assert.ok(
			warned.some((line) => line.includes('input-meter-open-late-failure') && line.includes('Permission denied by system')),
			'the late refusal is recorded with its cause'
		);
	} finally {
		mock.timers.reset();
		warn.mock.restore();
		if (savedNav) Object.defineProperty(globalThis, 'navigator', savedNav);
		else delete globalThis.navigator;
	}
});

test('an aborted microphone open fails at once with its reason, and a grant that lands later is stopped', async () => {
	const stopped = [];
	let grant;
	let asked = 0;
	const savedNav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
	Object.defineProperty(globalThis, 'navigator', {
		configurable: true,
		value: { mediaDevices: { getUserMedia: () => ((asked += 1), new Promise((resolve) => (grant = resolve))) } }
	});
	try {
		const abort = new AbortController();
		const watch = startInputLevelWatch('mic', () => {}, abort.signal);
		let failed = false;
		watch.catch(() => (failed = true));
		for (let i = 0; i < 5; i += 1) await Promise.resolve();
		// Control: an open nobody aborted keeps waiting on its prompt.
		assert.equal(failed, false);
		abort.abort(new Error('The performance view closed during the Test.'));
		await assert.rejects(watch, /performance view closed during the Test/);
		grant({ getTracks: () => [{ stop: () => stopped.push('track') }] });
		for (let i = 0; i < 5; i += 1) await Promise.resolve();
		assert.deepEqual(stopped, ['track'], 'a grant after the abort must not leave the mic open');
		// Already aborted: nothing is asked of the browser at all.
		await assert.rejects(startInputLevelWatch('mic', () => {}, abort.signal), /performance view closed/);
		assert.equal(asked, 1);
	} finally {
		if (savedNav) Object.defineProperty(globalThis, 'navigator', savedNav);
		else delete globalThis.navigator;
	}
});

// [if] the two-output CUE's liveness recovery starts re-binding it while a CUE Test sounds [then] that Test is cut, while a Test on MAIN plays on
test("a CUE output's liveness recovery cuts a CUE Test and leaves a MAIN Test sounding (Codex P2 BLOCKING on #6835)", () => {
	for (const bus of ['master', 'cue', 'input']) {
		for (const mode of ['one_output', 'two_outputs', 'split_cable']) {
			assert.equal(outputMoveCutsTest(undefined, bus, mode), true, `every other move cuts: ${bus} ${mode}`);
		}
	}
	assert.equal(outputMoveCutsTest('cue', 'cue', 'two_outputs'), true);
	assert.equal(outputMoveCutsTest('cue', 'master', 'two_outputs'), false);
	assert.equal(outputMoveCutsTest('cue', 'input', 'two_outputs'), false);
	assert.equal(outputMoveCutsTest('cue', 'cue', 'split_cable'), false, 'a split CUE Test plays on MAIN');
	// Wiring: the recovery announces itself before it suspends the cue, and the Test asks the filter.
	const hpSource = readFileSync(new URL('../../src/lib/player/headphones.ts', import.meta.url), 'utf8');
	const rebind = hpSource.slice(hpSource.indexOf('async function _rebindCueContext('));
	assert.ok(rebind.indexOf("_outputMoveStarted('cue');") !== -1 && rebind.indexOf("_outputMoveStarted('cue');") < rebind.indexOf('cue.suspend()'));
	const audioSource = readFileSync(new URL('../../src/lib/player/io-device-test-audio.ts', import.meta.url), 'utf8');
	assert.match(audioSource, /onOutputMoveStarted\(\(only\) => \{\n\t\tif \(generation !== beepGeneration\) return;\n\t\tif \(!outputMoveCutsTest\(only, bus, mixerState\.headphones\.output_mode\)\) return;/);
});

test('a Test refuses a live graph routed somewhere other than the device shown', () => {
	const outputs = [
		{ id: 'usb-a', label: 'USB A' },
		{ id: 'usb-b', label: 'USB B' },
		{ id: 'phones', label: 'Phones' }
	];
	const hp = (output_mode, master = 'selected', cue = 'selected') => ({ output_mode, outputs, routes: { master: { state: master }, cue: { state: cue } } });
	// MAIN shown on B after overlapping picks, sink still on A: refused, for MAIN and AUDIO IN.
	assert.match(liveRouteRefusal('master', 'usb-b', hp('two_outputs'), 'usb-a', 'phones'), /MAIN is still playing on another device/);
	assert.match(liveRouteRefusal('input', 'usb-b', hp('two_outputs'), 'usb-a', 'phones'), /MAIN is still playing on another device/);
	assert.match(liveRouteRefusal('master', 'usb-b', hp('two_outputs', 'failed'), 'usb-b', null), /MAIN did not open/);
	// A failed room reopen may leave the room `running` on a dead stream: MAIN
	// and AUDIO IN refuse even on the device shown, the OS default included,
	// while a CUE on its own output still plays (control).
	for (const bus of ['master', 'input']) {
		assert.match(liveRouteRefusal(bus, 'usb-b', hp('two_outputs'), 'usb-b', 'phones', true), /did not reopen/);
		assert.match(liveRouteRefusal(bus, null, hp('two_outputs'), null, 'phones', true), /did not reopen/);
	}
	assert.equal(liveRouteRefusal('cue', 'phones', hp('two_outputs'), 'usb-b', 'phones', true), null);
	assert.equal(liveRouteRefusal('master', 'usb-b', hp('two_outputs'), 'usb-b', 'phones', false), null);
	// A two-output CUE the liveness monitor found running but silent: a beep
	// would end on time and pass unheard, so it is refused (Codex P2 BLOCKING on #6835).
	const silent = (verdict) => ({ ...hp('two_outputs'), liveness_verdict: verdict });
	for (const verdict of ['stalled', 'dead', 'dead-escalated']) {
		assert.match(liveRouteRefusal('cue', 'phones', silent(verdict), 'usb-b', 'phones'), /not producing sound/, verdict);
		// Controls: MAIN is not the silent output, and in split L/R the CUE plays on MAIN.
		assert.equal(liveRouteRefusal('master', 'usb-b', silent(verdict), 'usb-b', 'phones'), null, verdict);
		assert.equal(liveRouteRefusal('cue', 'usb-b', { ...silent(verdict), output_mode: 'split_cable' }, 'usb-b', null), null, verdict);
	}
	for (const verdict of ['idle', 'ok', undefined]) {
		assert.equal(liveRouteRefusal('cue', 'phones', silent(verdict), 'usb-b', 'phones'), null, `control: ${verdict}`);
	}
	// CUE whose sink is on another device than the one shown, or whose route failed.
	assert.match(liveRouteRefusal('cue', 'phones', hp('two_outputs'), 'usb-a', 'usb-b'), /Headphone output is still on another device/);
	assert.match(liveRouteRefusal('cue', 'phones', hp('two_outputs', 'selected', 'failed'), 'usb-a', 'phones'), /Headphone output did not open/);
	// In split L/R the CUE beeps on MAIN, so MAIN's landing decides.
	assert.match(liveRouteRefusal('cue', 'usb-b', hp('split_cable'), 'usb-a', null), /MAIN is still playing/);
	// A split CUE beep enters ahead of MIX and LEVEL: MIX fully on MAIN or LEVEL
	// at zero would pass it unheard, so it is refused, whatever device is shown.
	const gain = (mix, level) => ({ ...hp('split_cable'), mix, level });
	assert.match(liveRouteRefusal('cue', 'usb-b', gain(1, 0.5), 'usb-b', null), /MIX or LEVEL is all the way down/);
	assert.match(liveRouteRefusal('cue', 'default', gain(0, 0), null, null), /MIX or LEVEL is all the way down/);
	// Controls: an audible cue gain plays, MAIN and AUDIO IN ignore the headphone
	// gain in split, and a two-output CUE (beeped after MIX and LEVEL) ignores it too.
	assert.equal(liveRouteRefusal('cue', 'usb-b', gain(0.99, 0.01), 'usb-b', null), null);
	assert.equal(liveRouteRefusal('master', 'usb-b', gain(1, 0), 'usb-b', null), null);
	assert.equal(liveRouteRefusal('input', 'usb-b', gain(1, 0), 'usb-b', null), null);
	assert.equal(liveRouteRefusal('cue', 'phones', { ...hp('two_outputs'), mix: 1, level: 0 }, 'usb-a', 'phones'), null);
	// A remount forgets the landing but keeps the pick: with a device shown, an
	// unknown landing may be the OS default, so it is refused as unverified.
	assert.match(liveRouteRefusal('master', 'usb-a', hp('two_outputs'), null, null), /MAIN has not landed on the picked device/);
	assert.match(liveRouteRefusal('input', 'usb-a', hp('two_outputs'), null, 'phones'), /MAIN has not landed/);
	assert.match(liveRouteRefusal('cue', 'usb-a', hp('split_cable'), null, null), /MAIN has not landed/);
	// Controls: the graph on the device shown, or no device shown (the OS default), plays.
	assert.equal(liveRouteRefusal('master', 'usb-a', hp('two_outputs'), 'usb-a', 'phones'), null);
	assert.equal(liveRouteRefusal('master', 'default', hp('two_outputs'), null, null), null);
	assert.equal(liveRouteRefusal('master', '', hp('one_output'), null, null), null);
	assert.equal(liveRouteRefusal('master', null, hp('one_output'), null, null), null);
	assert.equal(liveRouteRefusal('master', null, hp('one_output'), 'usb-a', null), null);
	assert.equal(liveRouteRefusal('cue', 'phones', hp('two_outputs'), 'usb-b', 'phones'), null);
	// A two-output CUE shown with no landing never opened: unverified, so refused.
	assert.match(liveRouteRefusal('cue', 'phones', hp('two_outputs'), 'usb-b', null), /Headphone output has not opened on the picked device/);
	assert.equal(liveRouteRefusal('cue', 'usb-a', hp('split_cable', 'selected', 'failed'), 'usb-a', null), null);
	// The live branch of the beep target asks it, with the real landing, CUE sink and reopen state, before it returns the graph.
	const audioSource = readFileSync(new URL('../../src/lib/player/io-device-test-audio.ts', import.meta.url), 'utf8');
	const live = audioSource.slice(audioSource.indexOf('if (live.ctx !== null && live.nodes !== null) {'));
	const ask = live.indexOf('liveRouteRefusal(bus, deviceId, hp, landedMasterSinkId(), live.nodes.cueDeviceId, mainReopenFailed())');
	assert.ok(ask > 0 && ask < live.indexOf('return { ctx: live.ctx'), 'the live graph is checked before a Test beeps it');
});

test('a Test on the live graph waits while its output is still switching', () => {
	const none = { master: false, cue: false };
	const master = { master: true, cue: false };
	const cue = { master: false, cue: true };
	// MAIN, AUDIO IN and split CUE beep on MAIN, so MAIN's move decides.
	assert.match(outputSwitchRefusal('master', 'two_outputs', master), /MAIN is still switching/);
	assert.match(outputSwitchRefusal('input', 'one_output', master), /MAIN is still switching/);
	assert.match(outputSwitchRefusal('cue', 'split_cable', master), /MAIN is still switching/);
	// Two-output CUE follows its own pick.
	assert.match(outputSwitchRefusal('cue', 'two_outputs', cue), /Headphone output is still switching/);
	// Controls: nothing pending plays, and the other bus's switch does not block.
	assert.equal(outputSwitchRefusal('master', 'two_outputs', none), null);
	assert.equal(outputSwitchRefusal('cue', 'two_outputs', none), null);
	assert.equal(outputSwitchRefusal('master', 'two_outputs', cue), null);
	assert.equal(outputSwitchRefusal('cue', 'two_outputs', master), null);
	assert.equal(outputSwitchRefusal('cue', 'split_cable', cue), null);
	// beepTarget asks it, with the real pending state, before it picks the live graph or a
	// short-lived context: a switch pending with no live graph must refuse too.
	const audioSource = readFileSync(new URL('../../src/lib/player/io-device-test-audio.ts', import.meta.url), 'utf8');
	const ask = audioSource.indexOf('outputSwitchRefusal(bus, hp.output_mode, outputSwitchesPending())');
	const graph = audioSource.indexOf('const live = liveCalibrationGraph();');
	assert.ok(ask > 0 && graph > 0 && ask < graph, 'a switching output is checked before a Test picks where it beeps');
	const hpSource = readFileSync(new URL('../../src/lib/player/headphones.ts', import.meta.url), 'utf8');
	assert.match(hpSource, /return \{ master: _masterMovesPending\(\) > 0, cue: _cuePicksInFlight > 0 \};/);
});

test('the passive input meter shows and records why it could not open, and stays quiet when it was closed', () => {
	const source = readFileSync(new URL('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte', import.meta.url), 'utf8');
	const passive = source.slice(source.indexOf('void inputRecheck;'), source.indexOf('function testDeviceId('));
	assert.doesNotMatch(passive, /catch \{/, 'no failure of the passive meter is swallowed');
	const failure = passive.slice(passive.indexOf('} catch (error) {'));
	const quiet = failure.indexOf('if (cancelled || opening.signal.aborted) return;');
	assert.ok(quiet > 0, 'a close or newer open is not reported as a failure');
	assert.ok(failure.indexOf('inputMeterError = reason;') > quiet, 'a real failure is shown');
	assert.ok(failure.indexOf("recordPerfEvent('io-input-meter-failed', reason") > quiet, 'and recorded');
	assert.match(passive, /return \(\) => \{[^}]*inputMeterError = null;/, 'closing the panel clears it');
	assert.match(source, /\{#if inputMeterError !== null\}<p class="hp-context" role="status" data-input-meter-error>Live input meter unavailable: \{inputMeterError\}<\/p>\{\/if\}/);
	assert.match(source, /return `input meter unavailable: \$\{inputMeterError\}`;/, "the meter's title says so too");
});

test('the panel says so when its Test did not finish playing, and stays quiet when it was cancelled', () => {
	const source = readFileSync(new URL('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte', import.meta.url), 'utf8');
	const play = source.slice(source.indexOf('async function playDeviceTest('));
	assert.match(play, /const played = await io\.playIoTestBeeps\(/);
	assert.match(play, /if \(!played && !local\.signal\.aborted && mine === testToken\) \{\s*testNote = 'The Test did not finish playing/);
});

test('a CUE Test refuses a two-output bridge whose receiver is not playing', () => {
	const bridge = { bridgeSender: {} };
	// A failed CUE pick and rollback leave the receiver suspended.
	assert.equal(liveCueSinkOpen({ ...bridge, cueContext: { state: 'suspended' } }, 'two_outputs', true), false);
	assert.equal(liveCueSinkOpen({ ...bridge, cueContext: { state: 'running' } }, 'two_outputs', false), false);
	assert.equal(liveCueSinkOpen({ ...bridge, cueContext: null }, 'two_outputs', true), false);
	// Control: a playing receiver, the native cue, split L/R and no bridge all sound.
	assert.equal(liveCueSinkOpen({ ...bridge, cueContext: { state: 'running' } }, 'two_outputs', true), true);
	assert.equal(liveCueSinkOpen({ ...bridge, cueContext: null, nativeCue: {} }, 'two_outputs', true), true);
	assert.equal(liveCueSinkOpen({ ...bridge, cueContext: { state: 'suspended' } }, 'split_cable', false), true);
	assert.equal(liveCueSinkOpen({ bridgeSender: null, cueContext: null }, 'practice', false), true);
	// A failed bridge worklet tears the bridge down but keeps the CUE pick:
	// two outputs then have no audible CUE path at all.
	assert.equal(liveCueSinkOpen({ bridgeSender: null, cueContext: null }, 'two_outputs', false), false);
	assert.equal(liveCueSinkOpen({ bridgeSender: null, cueContext: null, nativeCue: {} }, 'two_outputs', true), false);
	// beepTarget asks it before using the live graph for CUE.
	const source = readFileSync(new URL('../../src/lib/player/io-device-test-audio.ts', import.meta.url), 'utf8');
	const live = source.slice(source.indexOf('if (live.ctx !== null && live.nodes !== null)'));
	const guard = live.indexOf("bus === 'cue' && !liveCueSinkOpen(live.nodes, hp.output_mode, hp.active)");
	assert.ok(guard >= 0 && guard < live.indexOf('return {'), 'the CUE check must run before the live graph is used');
});

test('a Test whose short-lived context never closes still finishes', async () => {
	const savedCtx = globalThis.AudioContext;
	const param = () => ({ value: 0, cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime() {}, setTargetAtTime() {} });
	let osc;
	globalThis.AudioContext = class {
		state = 'running';
		currentTime = 0;
		destination = {};
		createOscillator() {
			osc = { frequency: param(), connect() {}, disconnect() {}, start() {}, stop() {}, onended: null };
			return osc;
		}
		createGain() {
			return { gain: param(), connect() {}, disconnect() {} };
		}
		close() {
			return new Promise(() => {});
		}
	};
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		let done = false;
		const test = playIoTestBeeps('master', null).then(() => (done = true));
		for (let i = 0; i < 10; i += 1) await Promise.resolve();
		osc.onended();
		for (let i = 0; i < 10; i += 1) await Promise.resolve();
		assert.equal(done, false, 'precondition: the close is pending');
		mock.timers.tick(5_000);
		await test;
		assert.equal(done, true, 'a close that never settles must not keep the Test busy');
	} finally {
		mock.timers.reset();
		globalThis.AudioContext = savedCtx;
	}
});

test('a cut test beep fades out before its nodes are released, and the Test ends only then', async () => {
	const savedCtx = globalThis.AudioContext;
	const param = () => ({ value: 0, cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime() {}, setTargetAtTime() {} });
	const released = [];
	let osc;
	let started;
	const begun = new Promise((resolve) => (started = resolve));
	globalThis.AudioContext = class {
		state = 'running';
		currentTime = 0;
		destination = {};
		createOscillator() {
			osc = { frequency: param(), connect() {}, disconnect: () => released.push('osc'), start() {}, stop() {}, onended: null };
			return osc;
		}
		createGain() {
			return { gain: param(), connect() {}, disconnect: () => released.push('gain') };
		}
		async close() {
			released.push('close');
		}
	};
	try {
		const beeping = playIoTestBeeps('master', null, () => started());
		await begun;
		let ended = false;
		void beeping.then(() => (ended = true));
		stopIoTestBeeps();
		await new Promise((r) => setTimeout(r, 5));
		assert.deepEqual(released, [], 'nothing is released while the fade still plays');
		assert.equal(ended, false, 'the Test still holds its owner while the fade plays');
		osc.onended();
		await beeping;
		assert.deepEqual(released, ['osc', 'gain', 'close'], 'released once the fade has ended');
		osc.onended();
		assert.equal(released.length, 3, 'released once');
	} finally {
		globalThis.AudioContext = savedCtx;
	}
});

test('a cut beep whose ended never fires is still released after the fade', async () => {
	const savedCtx = globalThis.AudioContext;
	const param = () => ({ value: 0, cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime() {}, setTargetAtTime() {} });
	const released = [];
	let started;
	const begun = new Promise((resolve) => (started = resolve));
	globalThis.AudioContext = class {
		state = 'running';
		currentTime = 0;
		destination = {};
		createOscillator() {
			return { frequency: param(), connect() {}, disconnect: () => released.push('osc'), start() {}, stop() {}, onended: null };
		}
		createGain() {
			return { gain: param(), connect() {}, disconnect: () => released.push('gain') };
		}
		async close() {
			released.push('close');
		}
	};
	try {
		const beeping = playIoTestBeeps('master', null, () => started());
		await begun;
		mock.timers.enable({ apis: ['setTimeout'] });
		let ended = false;
		void beeping.then(() => (ended = true));
		stopIoTestBeeps();
		await Promise.resolve();
		assert.deepEqual(released, []);
		assert.equal(ended, false);
		mock.timers.tick(1_000);
		await beeping;
		assert.deepEqual(released, ['osc', 'gain', 'close']);
	} finally {
		mock.timers.reset();
		globalThis.AudioContext = savedCtx;
	}
});

test('an AUDIO IN Test with no input picked is refused before any microphone opens, and a picked one is not', async () => {
	assert.match(ioTestRefusal('input', { selected_input_device_id: null }), /No audio input is picked/);
	assert.match(ioTestRefusal('input', { selected_input_device_id: '' }), /No audio input is picked/);
	assert.equal(ioTestRefusal('input', { selected_input_device_id: 'usb-mic' }), null);
	// MAIN with no pick is the OS default, where MAIN plays: tested, not refused.
	assert.equal(ioTestRefusal('master', { selected_input_device_id: null }), null);
	const asked = [];
	const savedNav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
	Object.defineProperty(globalThis, 'navigator', {
		configurable: true,
		value: { mediaDevices: { getUserMedia: async (c) => (asked.push(c), Promise.reject(new Error('stop after getUserMedia'))) } }
	});
	try {
		for (const none of [null, '']) {
			await assert.rejects(startInputLevelWatch(none, () => {}), /No audio input is picked/);
		}
		assert.equal(asked.length, 0, 'no microphone was asked for');
		// Control: a picked input asks for exactly that device.
		await assert.rejects(startInputLevelWatch('usb-mic', () => {}), /stop after getUserMedia/);
		assert.deepEqual(asked.map((c) => c.audio.deviceId), [{ exact: 'usb-mic' }]);
	} finally {
		if (savedNav) Object.defineProperty(globalThis, 'navigator', savedNav);
		else delete globalThis.navigator;
	}
});

test('the panel and the agent Test both refuse through ioTestRefusal before anything opens', () => {
	const panel = readFileSync(new URL('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte', import.meta.url), 'utf8');
	const play = panel.slice(panel.indexOf('async function playDeviceTest('));
	assert.ok(play.indexOf('ioTestRefusal(bus, headphoneState)') >= 0);
	assert.ok(play.indexOf('ioTestRefusal(bus, headphoneState)') < play.indexOf('loadIoAudio()'));
	const run = play.indexOf('await testrun(');
	assert.ok(run >= 0 && run < play.indexOf('io.playIoTestBeeps('), 'the panel beeps only inside the dispatcher-owned run');
	const mixer = readFileSync(new URL('../../src/lib/components/rb/Mixer.svelte', import.meta.url), 'utf8');
	assert.match(mixer, /testrun=\{runAudioIoTest\}/);
	const agent = readFileSync(new URL('../../src/lib/rb/agent-orders.ts', import.meta.url), 'utf8');
	const order = agent.slice(agent.indexOf('async function _ioDeviceTest('));
	assert.ok(order.indexOf('ioTestRefusal(testBus, headphones)') >= 0);
	assert.ok(order.indexOf('ioTestRefusal(testBus, headphones)') < order.indexOf('playIoTestBeeps('));
});

// [if] a Test fails with a value that is not an Error [then] the note shows
// that value, never a generic line that hides the cause.
test('a Test failure note keeps a non-Error cause', () => {
	const cluster = readFileSync(new URL('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte', import.meta.url), 'utf8');
	assert.match(cluster, /testNote = error instanceof Error \? error\.message : String\(error\);/);
	assert.doesNotMatch(cluster, /'Test beep failed'/);
});

// [if] an AUDIO IN Test runs [then] the passive meter does not hold a second
// capture of the same microphone; it reopens after the Test.
test('the passive input meter steps aside while an AUDIO IN Test runs', () => {
	const cluster = readFileSync(new URL('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte', import.meta.url), 'utf8');
	assert.match(cluster, /if \(!ioSurface\.open \|\| cueAlignModal\.open \|\| testBus === 'input'\) return;\n\t\tconst deviceId = headphoneState\.selected_input_device_id;/);
});

// [if] disconnecting a cut beep's nodes throws [then] the failure is recorded,
// never swallowed as "already disconnected".
test('a failed beep disconnect is recorded, not swallowed', () => {
	const audio = readFileSync(new URL('../../src/lib/player/io-device-test-audio.ts', import.meta.url), 'utf8');
	assert.doesNotMatch(audio, /\} catch \{\s*\/\/ already disconnected/);
	assert.equal(audio.match(/gain\.disconnect\(\);\s*\} catch \(error\) \{\s*recordPerfEvent\('io-test-beep-disconnect-failed'/g)?.length, 2, 'both beep cleanups record it');
});
