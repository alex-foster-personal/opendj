// requirement: IOPIN-19 (Audio I/O panel Test buttons and level meters)
// [if] an agent sends io_device_test for a bus [then] the same beeps play on the device the panel's Test uses, and the order answers once they sounded
// [if] the CUE Test has no CUE device picked, or the AUDIO IN Test no input [then] the order fails with the panel's reason, and nothing opens
// [if] the bus is not master, cue or input [then] the order fails before any audio opens
// [if] a second Test or a preset arrives while a Test holds the headphone scope [then] it waits for that Test
// [if] the route tears down mid-Test [then] the order fails, never answering as played
// [if] no route session is live, or a preset owns the lifecycle [then] the order fails before any audio opens
// [if] an AUDIO IN Test cannot open the picked microphone, or reads no level from it [then] the order fails
// [if] an agent Test is cut mid-pattern (the panel closing, another Test) after it sounded [then] the order fails, never answering as played
// [if] the route tears down while an AUDIO IN Test waits on the microphone permission prompt [then] the order fails at once, the headphone scope frees for the next mount, and a late grant is stopped
// [if] the route remounts while an agent Test is still loading its code [then] the order fails and sounds nothing on the replacement route
// [if] a Test starts while a torn-down agent Test's cut beep is still fading [then] the stale Test never cuts the new Test's beeps
// [if] MAIN changes while an agent AUDIO IN Test waits on the microphone [then] its beeps play on, and it answers with, the MAIN shown once the microphone opened
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const entry = await loadTypeScriptModule('tests/unit/fixtures/agent-orders-entry.ts');

const savedCtx = globalThis.AudioContext;
afterEach(() => {
	delete globalThis.window;
	globalThis.AudioContext = savedCtx;
	entry.mixerState.headphones.selected_input_device_id = null;
});

/** A Web Audio context whose beeps end at once; records every one it opens. */
function beepingContext(opened) {
	const param = () => ({ value: 0, cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime() {}, setTargetAtTime() {} });
	return class {
		state = 'running';
		currentTime = 0;
		destination = {};
		constructor() {
			opened.push(this);
		}
		createOscillator() {
			return {
				frequency: param(),
				connect() {},
				disconnect() {},
				start() {},
				stop() {
					queueMicrotask(() => this.onended?.());
				},
				onended: null
			};
		}
		createGain() {
			return { gain: param(), connect() {}, disconnect() {} };
		}
		async close() {
			this.state = 'closed';
		}
	};
}

const run = (command) => entry.executeAgentOrder({ kind: 'single', payload: command });

test('an agent MAIN Test plays the beeps and answers with the device it used', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const opened = [];
	globalThis.AudioContext = beepingContext(opened);
	const uninstall = entry.installPerformanceBrowserIpc();
	try {
		const result = await run({ type: 'io_device_test', bus: 'master' });
		assert.deepEqual(result.steps, [{ status: 'succeeded', result: { bus: 'master', device_id: null } }]);
		assert.equal(opened.length, 1, 'one short-lived test context');
		assert.equal(opened[0].state, 'closed');
	} finally {
		uninstall();
	}
});

test('an agent CUE or AUDIO IN Test with nothing picked fails with the reason and opens nothing; a bad bus is refused', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const opened = [];
	globalThis.AudioContext = beepingContext(opened);
	const uninstall = entry.installPerformanceBrowserIpc();
	try {
		const cue = await run({ type: 'io_device_test', bus: 'cue' });
		assert.equal(cue.steps[0].status, 'failed');
		assert.match(cue.steps[0].error, /No headphone output is picked/);
		const input = await run({ type: 'io_device_test', bus: 'input' });
		assert.equal(input.steps[0].status, 'failed');
		assert.match(input.steps[0].error, /No audio input is picked/);
		const bad = await run({ type: 'io_device_test', bus: 'room' });
		assert.equal(bad.steps[0].status, 'failed');
		assert.match(bad.steps[0].error, /bus must be master, cue or input/);
		assert.equal(opened.length, 0);
	} finally {
		uninstall();
	}
});

/** A context class whose first instance waits on its resume until released. */
function heldFirstContext(opened, onHeld) {
	const Base = beepingContext(opened);
	return class extends Base {
		constructor() {
			super();
			if (opened.length === 1) {
				this.state = 'suspended';
				this.resume = () =>
					new Promise((resolve) =>
						onHeld(() => {
							this.state = 'running';
							resolve();
						})
					);
			}
		}
	};
}

const settle = async (until) => {
	for (let i = 0; i < 50 && !until(); i += 1) await new Promise((r) => setTimeout(r, 1));
};

test('agent Tests hold the headphone scope: a second waits for the first, then both play', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const opened = [];
	let release;
	globalThis.AudioContext = heldFirstContext(opened, (r) => (release = r));
	const uninstall = entry.installPerformanceBrowserIpc();
	try {
		const first = run({ type: 'io_device_test', bus: 'master' });
		await settle(() => release !== undefined);
		let secondDone = false;
		const second = run({ type: 'io_device_test', bus: 'master' }).then((r) => ((secondDone = true), r));
		await settle(() => false);
		assert.equal(opened.length, 1, 'the second Test has not opened audio while the first holds the scope');
		assert.equal(secondDone, false);
		release();
		assert.equal((await first).steps[0].status, 'succeeded');
		assert.equal((await second).steps[0].status, 'succeeded');
		assert.equal(opened.length, 2);
	} finally {
		uninstall();
	}
});

test('a preset waits for an agent Test in flight, and a route torn down mid-Test fails the order', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const opened = [];
	let release;
	globalThis.AudioContext = heldFirstContext(opened, (r) => (release = r));
	let uninstall = entry.installPerformanceBrowserIpc();
	try {
		const test1 = run({ type: 'io_device_test', bus: 'master' });
		await settle(() => release !== undefined);
		let presetRan = false;
		const preset = entry.preparePerformancePresetTransaction('preset-after-test', async () => {
			presetRan = true;
		});
		await settle(() => false);
		assert.equal(presetRan, false, 'the preset waits for the Test holding the headphone scope');
		release();
		assert.equal((await test1).steps[0].status, 'succeeded');
		await preset;
		assert.equal(presetRan, true);
		entry.abortPreparedPerformancePreset('preset-after-test', 'test over');

		release = undefined;
		opened.length = 0;
		const test2 = run({ type: 'io_device_test', bus: 'master' });
		await settle(() => release !== undefined);
		uninstall();
		uninstall = () => {};
		release();
		const torn = await test2;
		assert.equal(torn.steps[0].status, 'failed', 'a Test whose route tore down never answers as played');
	} finally {
		uninstall();
	}
});

test('an agent Test with no route session, or while a preset owns the lifecycle, fails and opens nothing', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const opened = [];
	globalThis.AudioContext = beepingContext(opened);
	const torn = await run({ type: 'io_device_test', bus: 'master' });
	assert.equal(torn.steps[0].status, 'failed');
	assert.match(torn.steps[0].error, /performance view is not running/);
	const uninstall = entry.installPerformanceBrowserIpc();
	let release;
	const held = new Promise((resolve) => (release = resolve));
	const preset = entry.preparePerformancePresetTransaction('preset-under-test', () => held);
	try {
		const owned = await run({ type: 'io_device_test', bus: 'master' });
		assert.equal(owned.steps[0].status, 'failed');
		assert.match(owned.steps[0].error, /preset preset-under-test owns the audio/);
		assert.equal(opened.length, 0);
		release();
		await preset;
		entry.abortPreparedPerformancePreset('preset-under-test', 'test over');
		// Control: once the preset lets go, the same order plays.
		const after = await run({ type: 'io_device_test', bus: 'master' });
		assert.equal(after.steps[0].status, 'succeeded');
		assert.equal(opened.length, 1);
	} finally {
		release();
		await preset.catch(() => {});
		entry.abortPreparedPerformancePreset('preset-under-test', 'test over');
		uninstall();
	}
});

/** A picked microphone: getUserMedia per `open`, and a meter context whose
 * analyser reads `level`. Returns what it opened and stopped. */
function withMicrophone({ open, raf }, body) {
	const savedNav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
	const savedRaf = globalThis.requestAnimationFrame;
	const savedCancel = globalThis.cancelAnimationFrame;
	const stopped = [];
	Object.defineProperty(globalThis, 'navigator', {
		configurable: true,
		value: {
			mediaDevices: {
				getUserMedia: async (constraints) =>
					open(constraints, { getTracks: () => [{ stop: () => stopped.push('track') }] })
			}
		}
	});
	let frames = 0;
	globalThis.requestAnimationFrame = (fn) => {
		if (raf && frames < 3) {
			frames += 1;
			queueMicrotask(fn);
		}
		return frames;
	};
	globalThis.cancelAnimationFrame = () => {};
	return Promise.resolve(body(stopped)).finally(() => {
		if (savedNav) Object.defineProperty(globalThis, 'navigator', savedNav);
		else delete globalThis.navigator;
		globalThis.requestAnimationFrame = savedRaf;
		globalThis.cancelAnimationFrame = savedCancel;
	});
}

function meteringContext(opened, level) {
	const Base = beepingContext(opened);
	return class extends Base {
		createMediaStreamSource() {
			return { connect() {}, disconnect() {} };
		}
		createAnalyser() {
			return { fftSize: 256, getFloatTimeDomainData: (buf) => buf.fill(level) };
		}
	};
}

test('an agent AUDIO IN Test opens the picked microphone and answers with the level it read', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const opened = [];
	globalThis.AudioContext = meteringContext(opened, 0.25);
	const uninstall = entry.installPerformanceBrowserIpc();
	entry.mixerState.headphones.selected_input_device_id = 'mic-under-test';
	const asked = [];
	try {
		await withMicrophone({ open: (constraints, stream) => (asked.push(constraints), stream), raf: true }, async (stopped) => {
			const result = await run({ type: 'io_device_test', bus: 'input' });
			assert.equal(result.steps[0].status, 'succeeded', result.steps[0].error);
			assert.equal(result.steps[0].result.input_device_id, 'mic-under-test');
			assert.ok(Math.abs(result.steps[0].result.input_peak_rms - 0.25) < 1e-6);
			assert.deepEqual(asked[0].audio.deviceId, { exact: 'mic-under-test' });
			assert.deepEqual(stopped, ['track'], 'the microphone closes when the Test ends');
		});
	} finally {
		uninstall();
	}
});

test('an agent AUDIO IN Test fails when the microphone cannot open or no level is read', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const opened = [];
	globalThis.AudioContext = meteringContext(opened, 0.25);
	const uninstall = entry.installPerformanceBrowserIpc();
	entry.mixerState.headphones.selected_input_device_id = 'mic-under-test';
	try {
		await withMicrophone({ open: () => { throw new Error('NotFoundError: mic unplugged'); }, raf: true }, async () => {
			const result = await run({ type: 'io_device_test', bus: 'input' });
			assert.equal(result.steps[0].status, 'failed');
			assert.match(result.steps[0].error, /mic unplugged/);
			assert.equal(opened.length, 0, 'no beeps play for a microphone that did not open');
		});
		await withMicrophone({ open: (_c, stream) => stream, raf: false }, async (stopped) => {
			const result = await run({ type: 'io_device_test', bus: 'input' });
			assert.equal(result.steps[0].status, 'failed');
			assert.match(result.steps[0].error, /no level was read/);
			assert.deepEqual(stopped, ['track']);
		});
	} finally {
		uninstall();
	}
});

// [if] the microphone stops sending audio after one level was read (unplugged)
// [then] the AUDIO IN Test fails with the reason, never answers on its silence
test('an agent AUDIO IN Test fails when the microphone stops sending audio mid-Test (Codex P2 BLOCKING on #6835)', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const opened = [];
	const track = { readyState: 'live', stop() {} };
	const Base = meteringContext(opened, 0.25);
	globalThis.AudioContext = class extends Base {
		createAnalyser() {
			// One frame reads a level, then the device goes away.
			return { fftSize: 256, getFloatTimeDomainData: (buf) => (buf.fill(0.25), (track.readyState = 'ended')) };
		}
	};
	const uninstall = entry.installPerformanceBrowserIpc();
	entry.mixerState.headphones.selected_input_device_id = 'mic-under-test';
	try {
		await withMicrophone({ open: () => ({ getTracks: () => [track] }), raf: true }, async () => {
			const result = await run({ type: 'io_device_test', bus: 'input' });
			assert.equal(result.steps[0].status, 'failed', 'a level read before the microphone went away is no pass');
			assert.match(result.steps[0].error, /stopped sending audio/);
		});
	} finally {
		uninstall();
	}
});

test('a route torn down while an AUDIO IN Test waits on the mic prompt fails it at once, frees the scope, and stops a late grant', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const opened = [];
	globalThis.AudioContext = meteringContext(opened, 0.25);
	let uninstall = entry.installPerformanceBrowserIpc();
	entry.mixerState.headphones.selected_input_device_id = 'mic-under-test';
	let grant;
	try {
		await withMicrophone(
			{ open: (_c, stream) => new Promise((resolve) => (grant = () => resolve(stream))), raf: true },
			async (stopped) => {
				const pending = run({ type: 'io_device_test', bus: 'input' });
				await settle(() => grant !== undefined);
				assert.ok(grant !== undefined, 'the Test is waiting on the microphone prompt');
				uninstall();
				uninstall = entry.installPerformanceBrowserIpc();
				const startedAt = Date.now();
				const torn = await pending;
				assert.equal(torn.steps[0].status, 'failed');
				assert.match(torn.steps[0].error, /performance view closed/);
				assert.ok(Date.now() - startedAt < 1000, 'the open gave up at teardown, not at its 30 s bound');
				// The next mount's Test gets the headphone scope at once.
				const next = await run({ type: 'io_device_test', bus: 'master' });
				assert.equal(next.steps[0].status, 'succeeded', next.steps[0].error);
				// The prompt is answered after the route went: that stream stops.
				grant();
				await settle(() => stopped.length > 0);
				assert.deepEqual(stopped, ['track'], 'a grant landing after teardown is stopped');
				assert.equal(opened.length, 1, 'no meter context opened for the late grant, only the next Test');
			}
		);
	} finally {
		uninstall();
	}
});

test('an agent Test cut mid-pattern after it sounded fails, never answering as played', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const opened = [];
	// The scheduled stop never ends the tone here; only a cut (a second stop) does.
	const Base = beepingContext(opened);
	globalThis.AudioContext = class extends Base {
		createOscillator() {
			let calls = 0;
			const osc = super.createOscillator();
			osc.stop = () => {
				calls += 1;
				if (calls > 1) queueMicrotask(() => osc.onended?.());
			};
			return osc;
		}
	};
	const uninstall = entry.installPerformanceBrowserIpc();
	try {
		const pending = run({ type: 'io_device_test', bus: 'master' });
		await settle(() => opened.length > 0);
		await settle(() => false);
		entry.stopIoTestBeeps();
		const result = await pending;
		assert.equal(result.steps[0].status, 'failed');
		assert.match(result.steps[0].error, /cut before its beeps finished/);
	} finally {
		uninstall();
	}
});

test('an agent Test whose route remounts while its code loads fails and sounds nothing on the new route', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const opened = [];
	globalThis.AudioContext = beepingContext(opened);
	let uninstall = entry.installPerformanceBrowserIpc();
	try {
		const order = run({ type: 'io_device_test', bus: 'master' });
		// /performance unmounts and a replacement mounts before the Test's imports resolve.
		uninstall();
		uninstall = entry.installPerformanceBrowserIpc();
		const result = await order;
		assert.equal(result.steps[0].status, 'failed');
		assert.match(result.steps[0].error ?? JSON.stringify(result.steps[0]), /changed before the Test sounded/);
		assert.equal(opened.length, 0, 'nothing sounded on the replacement route');
	} finally {
		uninstall();
	}
});

test('a Test started while a torn-down agent Test is still fading out is never cut by it', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const opened = [];
	// The first Test's tone ends only when cut, and its fade-out then takes
	// 300 ms; every later Test's tone plays for 120 ms.
	const Base = beepingContext(opened);
	globalThis.AudioContext = class extends Base {
		createOscillator() {
			const osc = super.createOscillator();
			if (opened.length !== 1) {
				osc.stop = () => setTimeout(() => osc.onended?.(), 120);
				return osc;
			}
			let calls = 0;
			osc.stop = () => {
				calls += 1;
				if (calls > 1) setTimeout(() => osc.onended?.(), 300);
			};
			return osc;
		}
	};
	let uninstall = entry.installPerformanceBrowserIpc();
	try {
		const stale = run({ type: 'io_device_test', bus: 'master' });
		await settle(() => opened.length > 0);
		await settle(() => false);
		uninstall();
		uninstall = () => {};
		const replacement = await entry.playIoTestBeeps('master', null);
		assert.equal(replacement, true, 'the new Test played out, uncut by the stale one');
		assert.equal((await stale).steps[0].status, 'failed', 'the torn-down Test still never answers as played');
	} finally {
		uninstall();
	}
});

test('an agent AUDIO IN Test resolves MAIN after the microphone opens, not before', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const opened = [];
	const sinks = [];
	const Metering = meteringContext(opened, 0.25);
	globalThis.AudioContext = class extends Metering {
		async setSinkId(id) {
			sinks.push(id);
		}
	};
	const uninstall = entry.installPerformanceBrowserIpc();
	const state = entry.mixerState.headphones;
	const savedMain = state.selected_master_output_device_id;
	state.selected_input_device_id = 'mic-under-test';
	let grant;
	try {
		await withMicrophone(
			{ open: (_c, stream) => new Promise((resolve) => (grant = () => resolve(stream))), raf: true },
			async () => {
				const pending = run({ type: 'io_device_test', bus: 'input' });
				await settle(() => grant !== undefined);
				assert.ok(grant !== undefined, 'the Test is waiting on the microphone prompt');
				// A device change pins another MAIN while the prompt is open.
				state.selected_master_output_device_id = 'room-b';
				grant();
				const result = await pending;
				assert.equal(result.steps[0].status, 'succeeded', result.steps[0].error);
				assert.equal(result.steps[0].result.device_id, 'room-b', 'it answers for the MAIN shown once the microphone opened');
				assert.deepEqual(sinks, ['room-b'], 'and beeps there, never the MAIN it read before the prompt');
			}
		);
	} finally {
		state.selected_master_output_device_id = savedMain;
		uninstall();
	}
});
