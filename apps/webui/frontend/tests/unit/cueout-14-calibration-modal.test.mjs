// requirement: CUEOUT-14, IOPIN-07 (calibration state machine, injected effects, nothing performed)
// [if] the mic cannot hear the speakers [then] the modal fails at mic_check_master with the measured peak in the message, and paused decks resume
// [if] the operator closes the modal mid-measurement [then] mic tracks are stopped, chirps stopped, decks resumed, mixer-config unchanged
// [if] three runs spread more than 10 ms [then] nothing is applied
// [if] both checks pass and three runs agree [then] applied: persist receives the derived HEAD DELAY / room delay and the calibration record
// [if] the run is interactive [then] the cue chirp waits for Continue; headless never waits
// [if] getUserMedia is denied [then] failed with the browser's message and no deck was ever paused
// [if] POST /headphones/calibrate (IPC headphone_calibrate) runs while the page is open [then] the same state machine runs and calibration.step advances
// [if] IPC headphone_calibrate_abort lands mid-run [then] the run stops, decks resume, step returns to idle
import assert from 'node:assert/strict';
import { before, describe, test } from 'node:test';

import { addGaussianNoise, delayedCapture } from './cue-latency-eval.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

// 8 kHz keeps the real cross-correlation (the decision path under test) under
// a second for six measurements; the DSP is sample-rate agnostic.
const SAMPLE_RATE = 8000;

function installFakeWindow() {
	const store = new Map();
	globalThis.window = {
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	};
	return store;
}

/**
 * Injected effects. Every call is logged; nothing plays, records, pauses or
 * persists. `lags` names the acoustic lag per target per run; a `null` lag is
 * a capture the mic could not hear (noise only, peak well under 0.35).
 */
function harness({
	lags = { master: [200, 200, 200], cue: [900, 900, 900] },
	mode = 'hybrid',
	playing = [1, 3],
	gateAt = null,
	denyMic = false,
	/** Where the harness reads the live step from: its own object by default,
	 * the mirrored mixerState for the IPC tests. */
	stepSource = null
} = {}) {
	const calls = [];
	const signals = [];
	const runs = { master: 0, cue: 0 };
	let pendingRecord = null;
	let released = null;
	const gate = gateAt === null ? null : new Promise((resolve) => { released = resolve; });
	const stepsSeen = [];
	const calibration = { step: 'idle', cue_latency_ms: null, master_latency_ms: null, offset_ms: null, error: null };
	const liveStep = () => (stepSource === null ? calibration.step : stepSource());
	const mic = { stopped: 0, stop() { this.stopped += 1; calls.push('mic:stop'); } };
	const effects = {
		sampleRate: () => SAMPLE_RATE,
		async getUserMedia() {
			calls.push('getUserMedia');
			if (denyMic) throw new Error('NotAllowedError: Permission denied');
			return mic;
		},
		async playTrain(target, reference, sampleRate, signal) {
			calls.push(`play:${target}`);
			stepsSeen.push(liveStep());
			signals.push(signal);
			if (gateAt !== null && liveStep() === gateAt) {
				await new Promise((resolve, reject) => {
					signal.addEventListener('abort', () => { calls.push('play:stopped'); reject(new Error('chirp aborted')); });
					gate.then(resolve);
				});
			}
			const lag = lags[target][runs[target]];
			runs[target] += 1;
			const capture =
				lag === 'silent'
					? new Float32Array(reference.length + 4000)
					: lag === null
						? addGaussianNoise(new Float32Array(reference.length + 4000), 0.4, 7)
						: delayedCapture(reference, lag, sampleRate, 40);
			assert.ok(pendingRecord !== null, 'the controller must start recording BEFORE it plays the train');
			const resolve = pendingRecord;
			pendingRecord = null;
			resolve(capture);
		},
		record(handle, durationMs, sampleRate, signal) {
			assert.equal(handle, mic);
			assert.ok(durationMs > 0 && sampleRate === SAMPLE_RATE);
			calls.push('record');
			signals.push(signal);
			return new Promise((resolve, reject) => {
				pendingRecord = resolve;
				signal.addEventListener('abort', () => { calls.push('record:stopped'); reject(new Error('record aborted')); });
			});
		},
		async sleep(ms) { calls.push(`sleep:${ms}`); },
		async pauseDecks() { calls.push(`pause:${playing.join(',')}`); return [...playing]; },
		async resumeDecks(decks) { calls.push(`resume:${decks.join(',')}`); },
		now: () => Date.UTC(2026, 8, 15, 20, 0, 0),
		persist(result) { calls.push('persist'); effects.persisted = result; },
		alignmentMode: () => mode,
		deviceIds: () => ({ cue: 'bt-1', master: 'lg-hdmi' })
	};
	return { effects, calls, signals, calibration, mic, stepsSeen, release: () => released?.() };
}

async function settle() {
	for (let i = 0; i < 20; i += 1) await new Promise((r) => setImmediate(r));
}

let cueAlign;

before(async () => {
	installFakeWindow();
	cueAlign = await loadTypeScriptModule('src/lib/player/cue-align.svelte.ts');
});

describe('createCueAlignController', () => {
	test('happy path (hybrid, phones 700 ms behind): applied, room delayed 700, HEAD DELAY 0, decks resumed', async () => {
		const h = harness();
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'applied');
		assert.equal(h.calibration.master_latency_ms, 200);
		assert.equal(h.calibration.cue_latency_ms, 900);
		assert.equal(h.calibration.offset_ms, 700);
		assert.equal(h.calibration.error, null);
		assert.deepEqual(h.effects.persisted, {
			head_delay_ms: 0,
			master_delay_ms: 700,
			record: {
				cue_latency_ms: 900,
				master_latency_ms: 200,
				measured_at: '2026-09-15T20:00:00.000Z',
				cue_device_id: 'bt-1',
				master_device_id: 'lg-hdmi'
			}
		});
		assert.equal(h.calls.filter((c) => c === 'play:master').length, 3, 'three master runs');
		assert.equal(h.calls.filter((c) => c === 'play:cue').length, 3, 'three cue runs');
		assert.ok(h.calls.indexOf('pause:1,3') < h.calls.indexOf('play:master'), 'if decks are not paused before the first chirp then the room mix buries the measurement - broken');
		assert.ok(h.calls.indexOf('resume:1,3') > h.calls.lastIndexOf('play:cue'), 'decks resume after the last chirp');
		assert.equal(h.mic.stopped, 1, 'if the mic stream is left open then the tally light stays on after the modal closes - broken');
		assert.equal(h.calls.filter((c) => c === 'persist').length, 1);
	});

	test('uncorrelated captured input: failed at mic_check_master with measured correlation, decks resumed, cue never chirped', async () => {
		const h = harness({ lags: { master: [null, null, null], cue: [900, 900, 900] } });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'failed');
		assert.match(h.calibration.error, /captured input did not correlate with the speakers probe \(correlation 0\.\d+ < 0\.35; input peak \d\.\d{3}, rms 0\.\d{3}\)/,
			'if the failure does not carry measured correlation and input levels then the operator cannot tell no-input from an incorrect route - broken');
		assert.equal(h.calibration.diagnostics.failure, 'weak_correlation');
		assert.equal(h.calibration.master_latency_ms, null);
		assert.ok(!h.calls.includes('play:cue'), 'a failed speaker check must not go on to chirp the phones');
		assert.ok(h.calls.includes('resume:1,3'), 'if a failed check leaves the decks paused then calibration stopped the set - broken');
		assert.equal(h.mic.stopped, 1);
		assert.ok(!h.calls.includes('persist'));
	});

	test('uncorrelated headphone capture: failed at mic_check_cue naming the headphones', async () => {
		const h = harness({ lags: { master: [200, 200, 200], cue: [null, null, null] } });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'failed');
		assert.match(h.calibration.error, /captured input did not correlate with the headphones probe/);
		assert.equal(h.calibration.master_latency_ms, 200, 'the passing master check is kept for the report');
		assert.ok(h.calls.includes('resume:1,3'));
		assert.ok(!h.calls.includes('persist'));
	});

	test('silent capture is explicitly no-input, not blamed on room noise', async () => {
		const h = harness({ lags: { master: ['silent', 'silent', 'silent'], cue: [900, 900, 900] } });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'failed');
		assert.match(h.calibration.error, /No input signal was captured while checking the speakers \(input peak 0\.000\)/);
		assert.equal(h.calibration.diagnostics.failure, 'no_input_signal');
		assert.doesNotMatch(h.calibration.error, /room noise/i);
		assert.ok(!h.calls.includes('persist'));
	});

	test('three runs spreading more than 10 ms: failed as unstable, nothing applied', async () => {
		const h = harness({ lags: { master: [200, 200, 200], cue: [900, 915, 900] } });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'failed');
		assert.match(h.calibration.error, /inconsistent measurements \(spread 15 ms\); prior delay was kept/);
		assert.doesNotMatch(h.calibration.error, /room noise/i, 'the run did not observe room noise, only disagreeing measurements');
		assert.deepEqual(h.calibration.diagnostics, {
			probe: 'chirp',
			alternate_probe: 'unavailable',
			failure: 'inconsistent_measurements',
			master_measurements_ms: [200, 200, 200],
			cue_measurements_ms: [900, 915, 900],
			spread_ms: 15
		});
		assert.ok(!h.calls.includes('persist'), 'if an unstable measurement is applied then a noisy room writes a wrong room delay - broken');
		assert.ok(h.calls.includes('resume:1,3'));
	});

	test('a 10 ms spread is still accepted (the threshold is exclusive), taking the median', async () => {
		const h = harness({ lags: { master: [200, 210, 205], cue: [900, 900, 910] } });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'applied');
		assert.equal(h.calibration.master_latency_ms, 205);
		assert.equal(h.calibration.cue_latency_ms, 900);
		assert.equal(h.calibration.offset_ms, 695);
	});

	test('abort mid-measurement: chirps stopped, mic stopped, decks resumed, nothing persisted, step idle', async () => {
		const h = harness({ gateAt: 'measuring' });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		const run = controller.run({ interactive: false });
		for (let i = 0; i < 200 && h.calibration.step !== 'measuring'; i += 1) await settle();
		assert.equal(h.calibration.step, 'measuring', 'the harness must actually be parked mid-measurement, or this test proves nothing');
		controller.abort();
		await run;
		assert.equal(h.calibration.step, 'idle');
		assert.equal(h.calibration.error, null);
		assert.ok(h.signals.every((signal) => signal.aborted), 'if the abort signal is not raised then a chirp keeps playing into the phones after the modal closed - broken');
		assert.ok(h.calls.includes('play:stopped'));
		assert.ok(h.calls.includes('record:stopped'));
		assert.equal(h.mic.stopped, 1, 'if the mic is not stopped on abort then the tally light stays on - broken');
		assert.ok(h.calls.includes('resume:1,3'), 'if abort does not resume the decks it paused then closing the modal stops the set - broken');
		assert.ok(!h.calls.includes('persist'), 'if abort persists a partial result then mixer-config changes on a cancelled run - broken');
		assert.equal(controller.running(), false);
	});

	test('interactive run waits at mic_check_cue for Continue; headless never waits', async () => {
		const h = harness();
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		const run = controller.run({ interactive: true });
		for (let i = 0; i < 200 && h.calibration.step !== 'mic_check_cue'; i += 1) await settle();
		assert.equal(h.calibration.step, 'mic_check_cue');
		await settle();
		assert.ok(!h.calls.includes('play:cue'), 'if the cue chirp fires before Continue then the ear cup is not on the mic yet - broken');
		controller.continueCueCheck();
		await run;
		assert.equal(h.calibration.step, 'applied');

		const headless = harness();
		const headlessController = cueAlign.createCueAlignController(headless.effects, headless.calibration);
		await headlessController.run({ interactive: false });
		assert.equal(headless.calibration.step, 'applied', 'headless must run to completion with nobody pressing Continue');
	});

	test('getUserMedia denied: failed with the browser message, no deck was paused', async () => {
		const h = harness({ denyMic: true });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'failed');
		assert.match(h.calibration.error, /Microphone access failed: NotAllowedError: Permission denied/);
		assert.ok(!h.calls.some((c) => c.startsWith('pause:')), 'if decks are paused before the mic is granted then a denied prompt stops the set for nothing - broken');
		assert.ok(!h.calls.some((c) => c.startsWith('resume:')));
	});

	test('run while running rejects; abort when idle is a no-op', async () => {
		const h = harness({ gateAt: 'mic_check_master' });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		const run = controller.run({ interactive: false });
		for (let i = 0; i < 200 && h.calibration.step !== 'mic_check_master'; i += 1) await settle();
		await assert.rejects(controller.run({ interactive: false }), /already running/);
		h.release();
		await run;
		assert.equal(h.calibration.step, 'applied');
		controller.abort();
		assert.equal(h.calibration.step, 'applied', 'abort after completion must not erase the applied result');
	});

	test('a failed run resets to a fresh state on the next run', async () => {
		const h = harness({ lags: { master: [null, 200, 200, 200], cue: [900, 900, 900] } });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'failed');
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'applied');
		assert.equal(h.calibration.error, null);
	});
});

describe('IPC parity (headphone_calibrate / headphone_calibrate_abort drive the same machine)', () => {
	let ipc;
	let session;
	let headphones;

	before(async () => {
		installFakeWindow();
		ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
		session = await loadTypeScriptModule('src/lib/rb/cue-align-session.svelte.ts');
		headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	});

	const mirroredStep = () => ipc.queryPerformanceState().mixer.headphones.calibration.step;

	test('headphone_calibrate runs the state machine and calibration.step advances in the mirrored state', async () => {
		const h = harness({ stepSource: mirroredStep });
		h.effects.persist = (result) => {
			h.calls.push('persist');
			headphones.setHeadDelayMs(result.head_delay_ms);
			headphones.setMasterDelayMs(result.master_delay_ms);
		};
		session.overrideCueAlignEffects(() => h.effects);
		const uninstall = ipc.installPerformanceBrowserIpc();
		try {
			const before = ipc.queryPerformanceState().mixer.headphones.calibration.step;
			assert.equal(before, 'idle');
			await ipc.dispatchPerformanceCommand({ type: 'headphone_calibrate' });
			const after = ipc.queryPerformanceState().mixer.headphones;
			assert.equal(after.calibration.step, 'applied', 'if the IPC command does not drive the same machine then GET /headphones never sees the step move - broken');
			assert.equal(after.calibration.offset_ms, 700);
			assert.equal(after.master_delay_ms, 700);
			assert.deepEqual(h.stepsSeen.slice(0, 2), ['mic_check_master', 'mic_check_cue'], 'the steps the page publishes while the chirps play');
			assert.ok(h.stepsSeen.slice(2).every((step) => step === 'measuring'));
		} finally {
			session.overrideCueAlignEffects(null);
			uninstall();
		}
	});

	test('headphone_calibrate_abort stops a run in flight and the decks resume', async () => {
		const h = harness({ gateAt: 'measuring', stepSource: mirroredStep });
		session.overrideCueAlignEffects(() => h.effects);
		const uninstall = ipc.installPerformanceBrowserIpc();
		try {
			const run = ipc.dispatchPerformanceCommand({ type: 'headphone_calibrate' });
			for (let i = 0; i < 200 && ipc.queryPerformanceState().mixer.headphones.calibration.step !== 'measuring'; i += 1) await settle();
			assert.equal(ipc.queryPerformanceState().mixer.headphones.calibration.step, 'measuring');
			await ipc.dispatchPerformanceCommand({ type: 'headphone_calibrate_abort' });
			await run;
			assert.equal(ipc.queryPerformanceState().mixer.headphones.calibration.step, 'idle');
			assert.ok(h.calls.includes('resume:1,3'));
			assert.ok(!h.calls.includes('persist'));
		} finally {
			session.overrideCueAlignEffects(null);
			uninstall();
		}
	});

	test('without a live monitor graph the real effects refuse rather than chirp into nothing', async () => {
		session.overrideCueAlignEffects(null);
		const uninstall = ipc.installPerformanceBrowserIpc();
		try {
			await assert.rejects(
				ipc.dispatchPerformanceCommand({ type: 'headphone_calibrate' }),
				/the audio graph is not built yet/,
				'if a headless calibrate with no monitor graph resolves then the HTTP caller reads 200 for a run that never happened - broken'
			);
			const calibration = ipc.queryPerformanceState().mixer.headphones.calibration;
			assert.equal(calibration.step, 'failed');
			// The graph is the cause here, so the message has to lead with it. Blaming
			// the output mode sent a reader to the I/O pane to fix something that was
			// not broken.
			assert.match(calibration.error, /the audio graph is not built yet/);
		} finally {
			uninstall();
		}
	});
});
