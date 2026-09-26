// requirement: CUEOUT-14 (calibration state machine, injected effects, nothing performed)
// [if] the mic cannot hear the speakers [then] the modal fails at mic_check_master with the measured peak in the message, and paused decks resume
// [if] a bus is only audible above some chirp level [then] stage one climbs the gain ladder, stops at the first comfortable rung, and stage two reuses that rung
// [if] a single stage-two capture falls short [then] one retry at full level rescues the run instead of refusing it
// [if] no rung clears the refusal line but some cleared it faintly [then] the best faint rung is used rather than the first
// [if] no rung of the ladder is heard [then] the failure says the ramp reached full level, and a dead acoustic path is named as routing rather than volume
// [if] the operator closes the modal mid-measurement [then] mic tracks are stopped, chirps stopped, decks resumed, mixer-config unchanged
// [if] three runs spread more than 10 ms [then] nothing is applied
// [if] both checks pass and three runs agree [then] applied: persist receives the derived HEAD DELAY / room delay and the calibration record
// [if] the run is interactive [then] the cue chirp waits for Continue; headless never waits
// [if] getUserMedia is denied [then] failed with the browser's message and no deck was ever paused
// [if] POST /headphones/calibrate (IPC headphone_calibrate) runs while the page is open [then] the same state machine runs and calibration.step advances
// [if] IPC headphone_calibrate_abort lands mid-run [then] the run stops, decks resume, step returns to idle
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
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
 * persists. `lags` names the acoustic lag per target per HEARD measurement:
 * the ramp's heard rung first, then the three stage-two captures.
 *
 * `hear` is the acoustic model the gain ramp is tested against: a chirp played
 * below `hear[target]` comes back as noise the mic could not hear (peak well
 * under 0.35) and does NOT consume a lag, exactly as a too-quiet bus behaves;
 * `null` is a bus that is never audible at any level. It is returned mutable so
 * a test can heal a bus between runs.
 *
 * `silent` is the OTHER way a bus goes unheard, and the two must not be
 * conflated: a digitally silent capture (dead input device, or a bus routed to
 * a device nobody is listening to) rather than a faint one buried in noise.
 */
function harness({
	lags = { master: [200, 200, 200, 200], cue: [900, 900, 900, 900] },
	hear = {},
	silent = {},
	mode = 'hybrid',
	playing = [1, 3],
	gateAt = null,
	denyMic = false,
	preRunDelays = { head_delay_ms: 0, master_delay_ms: 0 },
	verifyResiduals = null,
	/** A bus the mic stops hearing during verification (ear cup moved, output gone). */
	deafDuringVerify = null,
	/** Where the harness reads the live step from: its own object by default,
	 * the mirrored mixerState for the IPC tests. */
	stepSource = null
} = {}) {
	const calls = [];
	const signals = [];
	// The shared clock, in seconds. It advances only where a real one would, so
	// the controller's "where should the chirp be in this capture" arithmetic
	// is exercised rather than trivially zero.
	let clockSec = 4.2;
	const gains = { master: [], cue: [] };
	const hearAt = { master: 0, cue: 0, ...hear };
	const silentAt = { master: false, cue: false, ...silent };
	const runs = { master: 0, cue: 0 };
	let pendingCapture = null;
	let scheduledSec = 0;
	let captureOpens = 0;
	let released = null;
	const gate = gateAt === null ? null : new Promise((resolve) => { released = resolve; });
	const stepsSeen = [];
	const calibration = {
		step: 'idle',
		cue_latency_ms: null,
		master_latency_ms: null,
		offset_ms: null,
		verify_residual_ms: null,
		probe: null,
		error: null
	};
	let appliedDelays = { ...preRunDelays };
	let verifyAttempt = 0;
	let pendingVerifyMasterLag = null;
	const liveStep = () => (stepSource === null ? calibration.step : stepSource());
	const mic = { stopped: 0, stop() { this.stopped += 1; calls.push('mic:stop'); } };
	const effects = {
		sampleRate: () => SAMPLE_RATE,
		async getUserMedia() {
			calls.push('getUserMedia');
			if (denyMic) throw new Error('NotAllowedError: Permission denied');
			return mic;
		},
		async playTrain(target, reference, sampleRate, signal, gain, whenSec, purpose = 'measure') {
			calls.push(`play:${target}:${purpose}`);
			gains[target].push(gain);
			assert.ok(gain > 0 && gain <= 1, `chirp gain must be in (0, 1], got ${gain}`);
			assert.ok(
				pendingCapture !== null,
				'the controller must open the capture and wait for it to be LIVE before it plays - broken'
			);
			assert.ok(
				whenSec > pendingCapture.startedAtSec,
				'a chirp scheduled before the capture started can never be in it - broken'
			);
			scheduledSec = whenSec;
			stepsSeen.push(liveStep());
			signals.push(signal);
			if (gateAt !== null && liveStep() === gateAt) {
				await new Promise((resolve, reject) => {
					signal.addEventListener('abort', () => { calls.push('play:stopped'); reject(new Error('chirp aborted')); });
					gate.then(resolve);
				});
			}
			const audible =
				hearAt[target] !== null && gain >= hearAt[target] && !(purpose === 'verify' && deafDuringVerify === target);
			let capture;
			if (audible) {
				const lag = lags[target][runs[target]];
				runs[target] += 1;
				let lagMs = lag;
				if (purpose === 'verify') {
					const base = lags[target][3];
					if (target === 'master') {
						lagMs = base + appliedDelays.master_delay_ms;
						pendingVerifyMasterLag = lagMs;
					} else {
						lagMs =
							verifyResiduals !== null
								? pendingVerifyMasterLag + verifyResiduals[verifyAttempt++]
								: base + appliedDelays.head_delay_ms;
					}
				}
				capture = delayedCapture(reference, lagMs, sampleRate, 40);
			} else if (silentAt[target]) {
				capture = new Float32Array(reference.length + 4000);
			} else {
				capture = addGaussianNoise(new Float32Array(reference.length + 4000), 0.4, 7);
			}
			// The capture the harness hands back is the reference delayed by
			// `lag` from the moment the chirp was SCHEDULED, which is what a
			// shared clock means; the controller must subtract the schedule to
			// recover `lag`.
			const scheduleFrames = Math.round((scheduledSec - pendingCapture.startedAtSec) * sampleRate);
			const shifted = new Float32Array(capture.length + scheduleFrames);
			shifted.set(capture, scheduleFrames);
			const resolve = pendingCapture.resolve;
			pendingCapture = null;
			resolve(shifted);
		},
		async openCapture(handle, durationMs, sampleRate, signal) {
			assert.equal(handle, mic);
			assert.ok(durationMs > 0 && sampleRate === SAMPLE_RATE);
			calls.push('record');
			signals.push(signal);
			// A real capture does not start on the instant it is asked for, and
			// the whole point of the handshake is that the caller need not know
			// how long it took. So the fake takes a varying, non-zero time.
			clockSec += 0.11 + (captureOpens % 3) * 0.07;
			captureOpens += 1;
			const startedAtSec = clockSec;
			const samples = new Promise((resolve, reject) => {
				pendingCapture = { resolve, startedAtSec };
				signal.addEventListener('abort', () => { calls.push('record:stopped'); reject(new Error('record aborted')); });
			});
			samples.catch(() => undefined);
			return { startedAtSec, samples };
		},
		contextTimeSec: () => clockSec,
		async sleep(ms) { calls.push(`sleep:${ms}`); clockSec += ms / 1000; },
		async pauseDecks() { calls.push(`pause:${playing.join(',')}`); return [...playing]; },
		async resumeDecks(decks) { calls.push(`resume:${decks.join(',')}`); },
		now: () => Date.UTC(2026, 8, 15, 20, 0, 0),
		persist(result) {
			calls.push('persist');
			effects.persisted = result;
			appliedDelays = {
				head_delay_ms: result.head_delay_ms,
				master_delay_ms: result.master_delay_ms
			};
		},
		readDelays: () => ({ ...appliedDelays }),
		setDelays(delays) {
			calls.push(`setDelays:${delays.head_delay_ms},${delays.master_delay_ms}`);
			appliedDelays = { ...delays };
		},
		alignmentMode: () => mode,
		deviceIds: () => ({ cue: 'bt-1', master: 'lg-hdmi' })
	};
	return { effects, calls, signals, calibration, mic, stepsSeen, gains, hearAt, release: () => released?.(), appliedDelays: () => ({ ...appliedDelays }) };
}

async function settle() {
	for (let i = 0; i < 20; i += 1) await new Promise((r) => setImmediate(r));
}

let cueAlign;
let cueLatency;

before(async () => {
	installFakeWindow();
	cueAlign = await loadTypeScriptModule('src/lib/player/cue-align.svelte.ts');
	cueLatency = await loadTypeScriptModule('src/lib/player/cue-latency.ts');
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
		assert.equal(h.calibration.verify_residual_ms, 0,
			'if verification passes then the residual is carried on the applied state - broken');
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
		assert.equal(h.calls.filter((c) => c === 'play:master:measure').length, 4, 'one ramp chirp and three paired master captures');
		assert.equal(h.calls.filter((c) => c === 'play:cue:measure').length, 4, 'one ramp chirp and three paired cue captures');
		assert.equal(h.calls.filter((c) => c === 'play:master:verify').length, 1, 'one verifying master chirp');
		assert.equal(h.calls.filter((c) => c === 'play:cue:verify').length, 1, 'one verifying cue chirp');
		assert.ok(h.calls.indexOf('pause:1,3') < h.calls.indexOf('play:master:measure'), 'if decks are not paused before the first chirp then the room mix buries the measurement - broken');
		assert.ok(h.calls.indexOf('resume:1,3') > h.calls.lastIndexOf('play:cue:verify'), 'decks resume after the last chirp');
		assert.equal(h.mic.stopped, 1, 'if the mic stream is left open then the tally light stays on after the modal closes - broken');
		assert.equal(h.calls.filter((c) => c === 'persist').length, 1);
	});

	test('the mic cannot hear the speakers: failed at mic_check_master, peak in the message, decks resumed, cue never chirped', async () => {
		const h = harness({ hear: { master: null } });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'failed');
		assert.match(h.calibration.error, /The mic could not hear the speakers even at full chirp level \(best peak 0\.\d+, needs 0\.35\)/,
			'if the failure does not carry the measured peak then the operator cannot tell "too quiet" from "broken" - broken');
		assert.match(h.calibration.error, /Turn the macOS output volume up, or move the laptop closer to the speakers\./,
			'a bus heard faintly under noise is a volume problem and must be reported as one');
		assert.deepEqual(h.gains.master, [...cueLatency.CUE_LATENCY_GAIN_STEPS],
			'if the ramp gives up before full level then a bus that only needed more gain is failed - broken');
		assert.equal(h.calibration.master_latency_ms, null);
		assert.ok(!h.calls.includes('play:cue:measure'), 'a failed speaker check must not go on to chirp the phones');
		assert.ok(h.calls.includes('resume:1,3'), 'if a failed check leaves the decks paused then calibration stopped the set - broken');
		assert.equal(h.mic.stopped, 1);
		assert.ok(!h.calls.includes('persist'));
	});

	test('the mic cannot hear the headphones: failed at mic_check_cue naming the headphones', async () => {
		const h = harness({ hear: { cue: null } });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'failed');
		assert.match(h.calibration.error, /The mic could not hear the headphones even at full chirp level/);
		assert.equal(h.calibration.master_latency_ms, 200, 'the passing master check is kept for the report');
		assert.ok(h.calls.includes('resume:1,3'));
		assert.ok(!h.calls.includes('persist'));
	});

	test('a digitally silent capture is reported as routing, not as volume', async () => {
		const h = harness({ hear: { master: null }, silent: { master: true } });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'failed');
		assert.match(h.calibration.error, /best peak 0\.00/);
		assert.match(h.calibration.error, /Nothing at all reached the microphone, so this is not a volume problem\./,
			'if a dead acoustic path is reported as "turn it up" then the operator raises the volume of a device nobody is listening to - broken');
		assert.match(h.calibration.error, /the room output really is the speakers and not a headphone jack/,
			'the exact fault seen in the field: MASTER pointed at an unplugged jack, so no level could ever have helped');
	});

	test('a bus audible only above 0.5: stage one climbs to 0.5, stops there, and stage two reuses it', async () => {
		const h = harness({ hear: { cue: 0.5 } });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'applied', 'a cue bus that simply needed more level must calibrate, not fail');
		assert.deepEqual(h.gains.cue, [0.15, 0.3, 0.5, 0.5, 0.5, 0.5, 0.5],
			'if stage two does not reuse the rung stage one found then the three paired captures are inaudible again - broken');
		assert.deepEqual(h.gains.master, [0.15, 0.15, 0.15, 0.15, 0.15],
			'a bus heard on the first rung must never be chirped louder than it needs');
		assert.equal(h.calibration.cue_latency_ms, 900,
			'the chirp that ended the ramp is run one, so a level find must not throw its measurement away');
	});

	test('the probe observer reports every rung, every stage-two confirm, and the best peak so far', async () => {
		const seen = [];
		const h = harness({ hear: { cue: 0.5 } });
		h.effects.onProbe = (probe) => seen.push(probe);
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		const cueRungs = seen.filter((p) => p.bus === 'cue' && p.peak !== null).map((p) => p.gain);
		// Three ramp rungs, then the three stage-two captures at the rung it chose:
		// the bar stays live through stage two instead of freezing on the ramp.
		assert.deepEqual(cueRungs, [0.15, 0.3, 0.5, 0.5, 0.5, 0.5, 0.5]);
		assert.ok(seen.some((p) => p.peak === null), 'each rung is announced before it plays, or the bar only moves after the fact');
		const last = seen[seen.length - 1];
		assert.ok(last.best >= last.threshold, 'the final report of a successful ramp must show the threshold met');
		assert.ok(seen.every((p) => p.threshold === 0.35));
	});

	test('chooseLevelRung prefers headroom, falls back to the best faint rung, and refuses when nothing cleared', () => {
		const at = (gain, peak) => ({ gain, peak, lagMs: gain * 1000 });
		assert.deepEqual(
			cueAlign.chooseLevelRung([at(0.15, 0.2), at(0.3, 0.38), at(0.5, 0.62)]),
			{ gain: 0.5, lagMs: 500 },
			'a rung comfortably clear of the refusal line is what stage two has to live with'
		);
		assert.deepEqual(
			cueAlign.chooseLevelRung([at(0.15, 0.36), at(0.3, 0.44), at(0.5, 0.38)]),
			{ gain: 0.3, lagMs: 300 },
			'if the FIRST marginal rung were taken then a louder, better one measured right after it is thrown away - broken'
		);
		assert.equal(cueAlign.chooseLevelRung([at(0.15, 0.1), at(1, 0.34)]), null,
			'0.34 is below the refusal line and must not be accepted just because it was the best of a bad ramp');
		assert.equal(cueAlign.chooseLevelRung([]), null);
	});

	test('a stage-two capture that falls short is retried at full level rather than refusing the run', async () => {
		const h = harness({ hear: { cue: 0.5 } });
		// Stage one settles on 0.5; the bus then needs FULL level from the first
		// confirming chirp onward. Wed 16 Sep 2026 on the real modal the speakers
		// read 0.52 in the ramp and 0.34 one rung louder a few seconds later, so a
		// retry that only climbs one rung still refuses a bus that is plainly heard.
		let confirms = 0;
		const raise = h.effects.playTrain;
		h.effects.playTrain = async (target, ...rest) => {
			if (target === 'cue' && (rest[5] ?? 'measure') === 'measure') {
				confirms += 1;
				if (confirms > 3) h.hearAt.cue = 1;
			}
			return raise(target, ...rest);
		};
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'applied',
			'if one short capture refuses the whole run then a cup shifting for a moment costs the operator the entire calibration - broken');
		assert.ok(h.gains.cue.includes(1), 'the retry must go to full level, not creep up one rung');
		assert.ok(!h.gains.cue.includes(0.75), 'if the retry stops at the next rung then a marginal room is refused a level it could have used - broken');
	});

	test('three runs spreading more than 10 ms: failed as unstable, nothing applied', async () => {
		const h = harness({ lags: { master: [200, 200, 200, 200], cue: [900, 900, 915, 900] } });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'failed');
		assert.match(h.calibration.error, /measurement unstable \(spread 15 ms\), try again with less room noise/);
		assert.match(h.calibration.error, /\(speakers 200, 200, 200 ms; headphones 900, 915, 900 ms\)/,
			'if the unstable error hides the per-bus lags then nobody can tell which bus wandered - broken');
		assert.ok(!h.calls.includes('persist'), 'if an unstable measurement is applied then a noisy room writes a wrong room delay - broken');
		assert.ok(h.calls.includes('resume:1,3'));
	});

	test('a 10 ms spread in the per-pair offset is still accepted (the threshold is exclusive), taking the median offset', async () => {
		const h = harness({ lags: { master: [200, 200, 210, 205], cue: [900, 900, 915, 910] } });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'applied');
		assert.equal(h.calibration.master_latency_ms, 205);
		assert.equal(h.calibration.cue_latency_ms, 910);
		assert.equal(h.calibration.offset_ms, 705, 'offsets 700, 705, 705: the applied offset is their median');
	});

	test('a drift common to both buses is accepted, because it cancels in the offset', async () => {
		// Measured Wed 16 Sep 2026 on the real modal with the sweep probe: the shared
		// mic input crept about 7 ms per capture on BOTH buses while cue minus
		// master held within 3 ms.
		const h = harness({ lags: { master: [140, 151, 159, 165], cue: [180, 190, 195, 201] } });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'applied',
			'if a mic-side drift both buses share refuses the run then calibration fails on a stable offset - broken');
		assert.equal(h.calibration.offset_ms, 36);
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
		assert.ok(!h.calls.includes('play:cue:measure'), 'if the cue chirp fires before Continue then the ear cup is not on the mic yet - broken');
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
		const h = harness({ hear: { master: null } });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'failed');
		h.hearAt.master = 0;
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'applied');
		assert.equal(h.calibration.error, null);
	});

	test('verification residual 0 ms reaches applied and carries verify_residual_ms', async () => {
		const h = harness({ verifyResiduals: [0] });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'applied',
			'if verification residual is 0 ms then the run must finish applied - broken');
		assert.equal(h.calibration.verify_residual_ms, 0,
			'if verification passes then verify_residual_ms is recorded - broken');
	});

	test('verification inside tolerance on the first attempt passes without a retry', async () => {
		const h = harness({ verifyResiduals: [5] });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'applied');
		assert.equal(h.calibration.verify_residual_ms, 5);
		assert.equal(h.calls.filter((c) => c === 'play:master:verify').length, 1,
			'if the first verification is inside tolerance then only one verify pair runs - broken');
		assert.equal(h.calls.filter((c) => c.startsWith('setDelays:')).length, 1,
			'if the first verification passes then only the provisional apply sets delays, with no adjustment - broken');
		assert.equal(h.calls.filter((c) => c === 'persist').length, 1);
		assert.ok(h.calls.lastIndexOf('persist') > h.calls.lastIndexOf('play:cue:verify'),
			'if the plan is persisted before verification finishes then a failed or cancelled run leaves it in storage - broken');
	});

	test('verification outside tolerance triggers exactly one retry', async () => {
		const h = harness({ verifyResiduals: [15, 2] });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'applied');
		assert.equal(h.calibration.verify_residual_ms, 2);
		assert.equal(h.calls.filter((c) => c === 'play:master:verify').length, 2,
			'if the first verification fails then exactly one retry pair runs, not more - broken');
		assert.equal(h.calls.filter((c) => c.startsWith('setDelays:')).length, 2,
			'if the first verification fails then the plan is adjusted once (after the provisional apply) before the second try - broken');
		const stored = h.effects.persisted.record;
		assert.equal(stored.cue_latency_ms - stored.master_latency_ms, h.calibration.offset_ms,
			'if the stored record does not carry the corrected offset then a later mode change re-applies the uncorrected one - broken');
		assert.equal(h.calibration.cue_latency_ms - h.calibration.master_latency_ms, h.calibration.offset_ms,
			'if the live cue latency is not corrected with the offset then the modal and GET /headphones report contradictory figures - broken');
		const baseline = harness({ verifyResiduals: [0] });
		await cueAlign.createCueAlignController(baseline.effects, baseline.calibration).run({ interactive: false });
		assert.equal(h.calibration.offset_ms, baseline.calibration.offset_ms + 15,
			'if the retry does not move the stored offset by the first residual then the correction is lost - broken');
	});

	test('two failed verifications revert both delays, fail, and do not persist after revert', async () => {
		const h = harness({
			preRunDelays: { head_delay_ms: 12, master_delay_ms: 34 },
			verifyResiduals: [15, 20]
		});
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'failed',
			'if both verification attempts fail then the run ends failed - broken');
		assert.match(h.calibration.error, /the fix did not hold after verification \(attempt 1: 15 ms off, attempt 2: 20 ms off\)/,
			'if verification fails twice then both residuals are reported - broken');
		assert.deepEqual(h.appliedDelays(), { head_delay_ms: 12, master_delay_ms: 34 },
			'if verification fails twice then both HEAD DELAY and ROOM delay revert to pre-run values - broken');
		assert.equal(h.calls.filter((c) => c === 'persist').length, 0,
			'if verification fails twice then nothing from the failed run reaches storage - broken');
		assert.ok(h.calls.includes('resume:1,3'));
	});

	test('headphones_only leaves a known residual and verification accepts it rather than failing', async () => {
		const h = harness({
			mode: 'headphones_only',
			lags: { master: [200, 200, 200, 200], cue: [230, 230, 230, 230] }
		});
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'applied',
			'if verification expects zero residual from a plan that deliberately delays nothing then headphones_only can never save - broken');
		assert.equal(h.calibration.offset_ms, 30);
		assert.equal(h.calls.filter((c) => c === 'play:master:verify').length, 1,
			'if the intended residual triggers a retry then the retry chases a gap the mode chose to leave - broken');
		const stored = h.effects.persisted;
		assert.equal(stored.record.cue_latency_ms - stored.record.master_latency_ms, 30,
			'if the stored offset is not the measured one then switching mode later cannot reuse it - broken');
		assert.deepEqual([stored.head_delay_ms, stored.master_delay_ms], [0, 0]);
	});

	test('a verification chirp below the peak threshold fails the run instead of trusting a noise lag', async () => {
		const h = harness({ deafDuringVerify: 'cue', preRunDelays: { head_delay_ms: 7, master_delay_ms: 9 } });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.equal(h.calibration.step, 'failed',
			'if an unheard verification chirp is scored then noise can pass or drive a bogus correction - broken');
		assert.match(h.calibration.error, /verification could not hear the headphones/);
		assert.deepEqual(h.appliedDelays(), { head_delay_ms: 7, master_delay_ms: 9 },
			'if a failed verification leaves the provisional delays applied then a failed run changes the mix - broken');
		assert.equal(h.calls.filter((c) => c === 'persist').length, 0);
		assert.ok(h.calls.includes('resume:1,3'));
	});

	test('verification chirps use injection points upstream of the delay nodes', async () => {
		const h = harness();
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		await controller.run({ interactive: false });
		assert.ok(h.calls.includes('play:master:verify'), 'if verify never chirps master then the loop is hollow - broken');
		assert.ok(h.calls.includes('play:cue:verify'), 'if verify never chirps cue then the loop is hollow - broken');
		const calibrationAudio = await loadTypeScriptModule('src/lib/player/cue-align-audio.ts');
		assert.equal(calibrationAudio.cueAlignChirpTargetKey('master', 'verify'), 'masterDelayInput',
			'if master verify chirps destination then ROOM delay is bypassed; if it chirps the mute gain then a muted room fails verification - broken');
		assert.equal(calibrationAudio.cueAlignChirpTargetKey('cue', 'verify'), 'headDelayInput',
			'if cue verify chirps bridgeInput then HEAD DELAY is bypassed; if it chirps cueMix then MIX at full MASTER silences it - broken');
		const source = readFileSync(new URL('../../src/lib/player/cue-align-audio.ts', import.meta.url), 'utf8');
		assert.match(source, /key === 'headDelayInput'\)[\s\S]*?return nodes\.delay;/,
			'if the cue verify chirp is not fed straight into HEAD DELAY then the user MIX and LEVEL gains can silence it - broken');
		assert.match(source, /key === 'masterDelayInput'\)[\s\S]*?masterDelayNode\(\)/,
			'if the master verify chirp is not fed straight into ROOM delay then master mute can silence it - broken');
		assert.match(source, /setDelays\(delays\) \{\s*applyUnsavedAlignmentDelays\(delays\);/,
			'if provisional delays go through the persisting setters then a reload mid-verification keeps an unverified plan - broken');
		assert.match(source, /startedAtSec: event\.playbackTime/,
			'if the capture is stamped with currentTime at callback time then main-thread jitter becomes measured latency - broken');
		assert.doesNotMatch(source, /startedAtSec: ctx\.currentTime/);
		assert.equal(calibrationAudio.cueAlignChirpTargetKey('master', 'measure'), 'destination');
		assert.equal(calibrationAudio.cueAlignChirpTargetKey('cue', 'measure'), 'bridgeInput');
	});

	test('abort during verifying restores pre-run delays and resumes the decks', async () => {
		const h = harness({ gateAt: 'verifying', preRunDelays: { head_delay_ms: 11, master_delay_ms: 22 } });
		const controller = cueAlign.createCueAlignController(h.effects, h.calibration);
		const run = controller.run({ interactive: false });
		for (let i = 0; i < 200 && h.calibration.step !== 'verifying'; i += 1) await settle();
		assert.equal(h.calibration.step, 'verifying', 'the harness must actually be parked mid-verification, or this test proves nothing');
		controller.abort();
		await run;
		assert.equal(h.calibration.step, 'idle');
		assert.equal(h.calibration.error, null);
		assert.deepEqual(h.appliedDelays(), { head_delay_ms: 11, master_delay_ms: 22 },
			'if abort during verifying leaves the applied delays in place then a cancelled run changes the mix - broken');
		assert.ok(h.signals.every((signal) => signal.aborted),
			'if the abort signal is not raised then a chirp keeps playing into the phones after the modal closed - broken');
		assert.ok(h.calls.includes('play:stopped'));
		assert.ok(h.calls.includes('record:stopped'));
		assert.equal(h.mic.stopped, 1, 'if the mic is not stopped on abort then the tally light stays on - broken');
		assert.ok(h.calls.includes('resume:1,3'), 'if abort does not resume the decks it paused then closing the modal stops the set - broken');
		assert.equal(h.calls.filter((c) => c === 'persist').length, 0,
			'if a run cancelled during verifying reaches storage then a later mode change re-applies an unverified result - broken');
		assert.equal(controller.running(), false);
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
		const basePersist = h.effects.persist;
		h.effects.persist = (result) => {
			basePersist(result);
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
			// Everything after the two mic checks is either the stage-one/two
			// measurement chirps or the post-apply verification chirp; both are
			// real steps the page can render, unlike a bare 'idle' or 'applied'.
			assert.ok(h.stepsSeen.slice(2).every((step) => step === 'measuring' || step === 'verifying'));
		} finally {
			session.overrideCueAlignEffects(null);
			uninstall();
		}
	});

	test('the live stage-one probe reaches queryPerformanceState, in the read model\'s snake_case', async () => {
		const h = harness({ gateAt: 'measuring', stepSource: mirroredStep });
		const seen = [];
		h.effects.onProbe = (probe) => seen.push(probe);
		session.overrideCueAlignEffects(() => h.effects);
		const uninstall = ipc.installPerformanceBrowserIpc();
		try {
			assert.equal(ipc.queryPerformanceState().mixer.headphones.calibration.probe, null, 'no run, no probe');
			const run = ipc.dispatchPerformanceCommand({ type: 'headphone_calibrate' });
			for (let i = 0; i < 200 && ipc.queryPerformanceState().mixer.headphones.calibration.step !== 'measuring'; i += 1) await settle();
			assert.ok(seen.length > 0, 'the harness must actually publish probes, or the next assertion proves nothing');
			const last = seen[seen.length - 1];
			assert.deepEqual(
				{ ...ipc.queryPerformanceState().mixer.headphones.calibration.probe },
				{ bus: last.bus, gain: last.gain, peak: last.peak, lag_ms: last.lagMs, best: last.best, threshold: last.threshold },
				'if the probe the modal draws is not in queryPerformanceState then an agent drives the ear-cup step blind - broken'
			);
			await ipc.dispatchPerformanceCommand({ type: 'headphone_calibrate_abort' });
			await run;
			assert.equal(ipc.queryPerformanceState().mixer.headphones.calibration.probe, null,
				'if the probe outlives its run then an agent reads a stale level as live - broken');
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
