/**
 * The audio soak harness: the SHIPPED recovery modules, a real AudioContext,
 * real audio, and a modelled output device that can be taken away.
 *
 * ONE ENTRY, not several imports, for the same reason
 * `master-meter-browser-entry.ts` is one entry: `audio-output-rebind.ts` and
 * `audio-context-watchdog.ts` both keep module-level state (the deferred slot,
 * the stall listeners, the recovery listeners), so two bundles would give two
 * unrelated copies of it and the spec would drive one while asserting on the
 * other.
 *
 * WHAT IS REAL HERE, AND WHAT IS MODELLED. This distinction is the whole
 * honesty of the suite, so it is stated before anything else:
 *
 *   REAL - `installOutputRebind` and `installAudioContextWatchdog` exactly as
 *   shipped, including their debounce, cooldown, deferral, backoff and gates;
 *   a real `AudioContext`; a real `AudioBufferSourceNode` playing real
 *   samples; a real `AnalyserNode` measuring them; the real
 *   `SILENCE_RMS_FLOOR` the app's own silence watchdog uses; the real
 *   `meter-tap.ts` worklet when the spec arms it.
 *
 *   MODELLED - the OUTPUT DEVICE, as `deviceGate`. A browser cannot be made to
 *   lose a Bluetooth device on command from Playwright, so the one thing a
 *   test cannot have is the real failure. What is modelled is narrow and
 *   matches the shipped docstring's own account of the mechanism: the graph is
 *   bound to a device, a suspend/resume re-binds it to whatever the CURRENT
 *   device is, and audio reaches the speaker only while those two agree.
 *
 *   THE CONSEQUENCE, stated rather than left for a reader to work out (Sol P1
 *   BLOCKING, thread 3973204923): because `resume()` here sets
 *   `boundDevice = currentDevice`, this harness cannot tell that Chromium
 *   stayed bound to a dead PHYSICAL output. It measures whether the shipped
 *   DECISION path keeps signal arriving at the point the device would sit,
 *   which is where #1619's defect lived. Anything downstream of that point is
 *   UNAVAILABLE to this suite and is reported as unavailable, never as green.
 *
 *   REAL, NOT MODELLED (#1642, corrected after a P1 BLOCKING review on PR
 *   #1693): `foldDeviceLivenessSample`'s sole device-loss input,
 *   `outputLatencyDead`, is read from the shipped `installOutputLiveness`
 *   module (`audio-output-liveness.ts`) - the SAME third REAL module
 *   `audio-context-instrumentation.ts` installs in production, wired here
 *   exactly like `installOutputRebind` and `installAudioContextWatchdog`
 *   above, not a hand-rolled `ctx.outputLatency === 0` check. That
 *   distinction is load-bearing: an earlier revision of this harness read
 *   `outputLatency` raw, once per 100ms sample, bypassing
 *   `installOutputLiveness`'s own debounce (`LIVENESS_DEAD_POLLS`
 *   consecutive real `LIVENESS_POLL_MS` polls, 5s) entirely - and a raw
 *   read false-positived on the transient `outputLatency === 0` reading
 *   Chromium gives for a fraction of a second after `resume()`
 *   (`audio-output-liveness.ts`'s own docstring), the exact thing the
 *   debounce exists to absorb. Installing the real module rather than
 *   re-deriving its output is also why this harness no longer fabricates a
 *   `deviceClockStalled` signal from the SAME `deviceGate`/`stranded`/
 *   `deviceGone` variables that model the output device (a control that
 *   shares the defect under test is not a control -
 *   `.claude/rules/verification.md`); that fold input was dropped from
 *   `output-device-watchdog.ts` outright (see its docstring: the
 *   clock-stall requirement missed the real Wed 2 Sep 2026 incident, where
 *   the HAL clock kept advancing while `outputLatency` read dead).
 *
 *   THE CONSEQUENCE: this harness's real `AudioContext` never actually loses
 *   ITS device - Playwright cannot unpair a Bluetooth headset here any more
 *   than it can for `deviceGate` - so `outputLatency` never stays at 0 for a
 *   full debounce window. `outputLatencyDead` is therefore UNAVAILABLE in
 *   this environment: `deviceUnreachableVerdicts` and
 *   `longestDeviceUnreachableMs` stay at 0 for the whole run, asserted as
 *   such rather than left to look like an unexercised gap. The fold's
 *   DECISION logic is proven in `output-device-watchdog.test.mjs` against
 *   real inputs a live browser cannot supply here. What THIS harness proves
 *   instead: real telemetry, through the real debounced liveness module,
 *   reaches the shipped fold end-to-end, and it never false-positives
 *   `device-unreachable` while a genuinely healthy `outputLatency` runs
 *   through the same hostile schedule that flaps `deviceGate`.
 *
 * WHY THE PROBE IS DOWNSTREAM OF THE GATE, which is a finding in itself. The
 * app already has a master-bus silence watchdog (`master-silence-report.ts`)
 * and a master level meter (#1575), and NEITHER can see this class of failure:
 * both tap `_masterGain`, upstream of the output device, so during the
 * 17:44Z cutout they would have read a healthy signal the whole time the room
 * heard nothing. A device-level failure needs a device-level probe, so the
 * soak's analyser sits after the gate and answers "did sound reach the
 * speaker", not "did the mixer produce any".
 *
 * NOTHING IS AUDIBLE. The terminal sink is a zero gain, the same trick the
 * meter taps and the `?muted=1` headless mute node use. Every sample is really
 * rendered and really measured; only the last node to the speakers is silent.
 */

import {
	installAudioContextWatchdog,
	noteRecoveryOpportunity,
	CONTEXT_RESUME_BACKOFF_MS
} from '$lib/rb/audio-context-watchdog';
import {
	installOutputRebind,
	REBIND_COOLDOWN_MS,
	REBIND_DEBOUNCE_MS
} from '$lib/rb/audio-output-rebind';
import { installOutputLiveness, type LivenessVerdict } from '$lib/rb/audio-output-liveness';
import { SILENCE_RMS_FLOOR } from '$lib/rb/silence-watchdog';
import { foldDeviceLivenessSample } from '$lib/rb/output-device-watchdog';
import {
	attachMeterTaps,
	createMasterMeterSource,
	masterMeterNode,
	releaseMasterMeterTap,
	teardownMeterTaps
} from '$lib/rb/meter-tap';

export interface SoakProgress {
	elapsedMs: number;
	samples: number;
	/** Longest run of sub-floor RMS observed WHILE a deck was playing. */
	longestSilentWhilePlayingMs: number;
	/** Where that run started, so a failure can name the event that caused it. */
	longestSilentStartedAtMs: number;
	/** Currently accumulating a silent run, and since when. */
	silentSinceMs: number | null;
	events: string[];
	/**
	 * The NAME of every scheduled event that actually ran, in order.
	 *
	 * `events` is prose for a human reading a failure; this is identity, so the
	 * spec can assert that the schedule it CLAIMS to inject was injected. A
	 * count of prose lines cannot tell six flaps from one of each (Sol P1
	 * BLOCKING, PR #1644).
	 */
	eventsRan: string[];
	toasts: { message: string; kind: string }[];
	perf: { kind: string; message: string; severity: string }[];
	/** Highest RMS ever seen. A run that never rose above the floor measured nothing. */
	peakRms: number;
	/**
	 * Highest RMS seen UPSTREAM of the modelled device (at `masterGain`), the
	 * same tap point `master-silence-report.ts` uses. Proves the #1642 claim
	 * that the graph kept producing signal even while `peakRms` (downstream)
	 * went quiet during a flap.
	 */
	peakUpstreamRms: number;
	/** Longest run where the mixer had signal but the debounced liveness verdict read the output dead. */
	longestDeviceUnreachableMs: number;
	deviceUnreachableStartedAtMs: number;
	deviceUnreachableSinceMs: number | null;
	/** How many times the shipped `foldDeviceLivenessSample` actually reported the edge. */
	deviceUnreachableVerdicts: number;
	done: boolean;
	error: string | null;
}

export interface SoakOptions {
	durationMs: number;
	/** Sampling period for the analyser probe. */
	samplePeriodMs?: number;
	/**
	 * Close the modelled device permanently at this offset and never re-open it,
	 * bypassing the rebind path entirely. The NEGATIVE CONTROL: it proves the
	 * probe can report silence, so a clean run means "sound was there" rather
	 * than "the probe never worked".
	 */
	strandDeviceAtMs?: number;
}

export interface SoakHarness {
	start(options: SoakOptions): void;
	progress(): SoakProgress;
	stop(): Promise<void>;
	readonly constants: {
		SILENCE_RMS_FLOOR: number;
		REBIND_COOLDOWN_MS: number;
		REBIND_DEBOUNCE_MS: number;
		CONTEXT_RESUME_BACKOFF_MS: readonly number[];
	};
	/** Every hostile event this harness schedules, by name. */
	readonly scheduleNames: readonly string[];
}

declare global {
	interface Window {
		__audioSoakHarness?: SoakHarness;
	}
}

/** A `mediaDevices` stand-in the spec can fire `devicechange` on. */
function makeFakeMediaDevices(): {
	addEventListener(t: 'devicechange', h: () => void): void;
	removeEventListener(t: 'devicechange', h: () => void): void;
	fire(): void;
} {
	const handlers = new Set<() => void>();
	return {
		addEventListener: (_t, h) => void handlers.add(h),
		removeEventListener: (_t, h) => void handlers.delete(h),
		fire: () => {
			for (const h of handlers) h();
		}
	};
}

function makeToneBuffer(ctx: AudioContext): AudioBuffer {
	const seconds = 2;
	const buffer = ctx.createBuffer(1, ctx.sampleRate * seconds, ctx.sampleRate);
	const data = buffer.getChannelData(0);
	// 220 Hz at 0.5 amplitude: RMS ~0.354, which is ~350x SILENCE_RMS_FLOOR, so
	// the sound/silence decision is never a close call about a fading tail.
	for (let i = 0; i < data.length; i += 1) {
		data[i] = 0.5 * Math.sin((2 * Math.PI * 220 * i) / ctx.sampleRate);
	}
	return buffer;
}

function install(): void {
	const progress: SoakProgress = {
		elapsedMs: 0,
		samples: 0,
		longestSilentWhilePlayingMs: 0,
		longestSilentStartedAtMs: -1,
		silentSinceMs: null,
		events: [],
		eventsRan: [],
		toasts: [],
		perf: [],
		peakRms: 0,
		peakUpstreamRms: 0,
		longestDeviceUnreachableMs: 0,
		deviceUnreachableStartedAtMs: -1,
		deviceUnreachableSinceMs: null,
		deviceUnreachableVerdicts: 0,
		done: false,
		error: null
	};

	let stopRequested = false;
	let teardown: (() => Promise<void>) | null = null;
	/**
	 * Filled when `run()` builds the schedule, and read by the spec through the
	 * harness. A hardcoded list in the spec would be a COPY, and a copy that
	 * drifts asserts against events this file no longer has.
	 */
	let scheduleNames: readonly string[] = [];

	const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

	async function run(options: SoakOptions): Promise<void> {
		const samplePeriodMs = options.samplePeriodMs ?? 100;
		const ctx = new AudioContext();
		if (ctx.state === 'suspended') await ctx.resume();
		// Declared HERE, not beside the sampler: `note()` below is called during
		// meter arming, which happens before the probe is built, and a `const`
		// read from its own temporal dead zone throws rather than reporting.
		const t0 = performance.now();

		// ---- the graph -------------------------------------------------------
		const source = ctx.createBufferSource();
		source.buffer = makeToneBuffer(ctx);
		source.loop = true;
		const masterGain = ctx.createGain();
		masterGain.gain.value = 1;
		/** THE MODELLED OUTPUT DEVICE. Open only while bound === current. */
		const deviceGate = ctx.createGain();
		const analyser = ctx.createAnalyser();
		analyser.fftSize = 2048;
		/**
		 * UPSTREAM of `deviceGate`, at the same tap point `master-silence-report.ts`
		 * uses: measures "is the graph producing signal", which #1642's new claim
		 * needs kept SEPARATE from `analyser` above ("did sound reach the modelled
		 * speaker"). Reusing `analyser` for both would make the new instrument
		 * agree with the old one by construction, proving nothing distinct.
		 */
		const masterAnalyser = ctx.createAnalyser();
		masterAnalyser.fftSize = 2048;
		const silentSink = ctx.createGain();
		silentSink.gain.value = 0;
		source.connect(masterGain);
		masterGain.connect(deviceGate);
		masterGain.connect(masterAnalyser);
		deviceGate.connect(analyser);
		analyser.connect(silentSink);
		masterAnalyser.connect(silentSink);
		silentSink.connect(ctx.destination);
		source.start();

		let currentDevice = 'built-in';
		let boundDevice = 'built-in';
		let stranded = false;
		/**
		 * The device is GONE, so `resume()` itself rejects.
		 *
		 * Distinct from `stranded`, which models a graph bound to a device that
		 * went away while the context stayed healthy. This models the other half
		 * of the same incident: the context is not running and CANNOT be made to
		 * run, which is what makes the watchdog's bounded schedule run out
		 * instead of succeeding on its first 0ms attempt.
		 */
		let deviceGone = false;
		/**
		 * Resume attempts refused since the device went away, and the attempt
		 * number after which it comes back.
		 *
		 * COUNTED, NOT TIMED (Codex P2 BLOCKING, thread 3974276945). The first
		 * version of `recoveryEdgeAcrossPause` returned the device on a wall
		 * clock at 8s, which is BEFORE the original schedule's own final retry at
		 * 9,050ms -- so that retry succeeded on its own and the held opportunity
		 * was never needed. Reverting the fix under test left the soak green,
		 * which is the exact defect class this suite exists to guard, committed
		 * inside the suite itself.
		 *
		 * A count cannot drift: the original schedule can produce EXACTLY
		 * `CONTEXT_RESUME_BACKOFF_MS.length` attempts, so attempt N+1 can only
		 * come from a re-armed schedule. Returning the device after N+2 puts
		 * recovery strictly out of the first schedule's reach, on a loaded
		 * machine as much as an idle one.
		 */
		let resumeAttemptsWhileGone = 0;
		let returnDeviceAfterAttempt: number | null = null;
		const applyGate = (): void => {
			deviceGate.gain.value = !stranded && boundDevice === currentDevice ? 1 : 0;
		};
		applyGate();

		let deviceLivenessState: ReturnType<typeof foldDeviceLivenessSample> | undefined;

		let playing = true;
		const note = (text: string): void => {
			progress.events.push(`${Math.round(performance.now() - t0)}ms ${text}`);
		};

		// ---- the shipped modules, on a shim that models the device -----------
		//
		// `resume()` re-binds to the CURRENT device. That is the browser
		// behaviour the rebind module's docstring describes and relies on: "a
		// suspend/resume cycle, which makes the browser re-open its output
		// stream on the current default device".
		const listeners = new Set<() => void>();
		const shim = {
			get state(): string {
				return ctx.state;
			},
			async suspend(): Promise<void> {
				await ctx.suspend();
			},
			async resume(): Promise<void> {
				// A resume against a device that is not there REJECTS. The shipped
				// watchdog swallows that and retries, which is the behaviour the
				// re-arm path exists to outlast.
				if (deviceGone) {
					resumeAttemptsWhileGone += 1;
					// The device comes back AFTER this attempt has already failed,
					// never during it, so the attempt that trips the counter is not
					// the one that recovers.
					if (
						returnDeviceAfterAttempt !== null &&
						resumeAttemptsWhileGone >= returnDeviceAfterAttempt
					) {
						deviceGone = false;
						note(`the device comes back after resume attempt ${resumeAttemptsWhileGone}`);
					}
					throw new Error('soak: the output device is gone');
				}
				await ctx.resume();
				boundDevice = currentDevice;
				applyGate();
			},
			addEventListener: (_t: 'statechange', h: () => void): void => void listeners.add(h),
			removeEventListener: (_t: 'statechange', h: () => void): void => void listeners.delete(h)
		};
		ctx.addEventListener('statechange', () => {
			for (const h of listeners) h();
		});

		const media = makeFakeMediaDevices();
		const isAnyDeckPlaying = (): boolean => playing;

		const detachWatchdog = installAudioContextWatchdog(
			shim,
			{
				pushToast: (message, kind) => progress.toasts.push({ message, kind }),
				recordPerfTiming: () => {},
				sleep: (ms) => new Promise<void>((r) => setTimeout(r, ms))
			},
			isAnyDeckPlaying
		);

		const rebind = installOutputRebind(
			shim,
			{
				pushToast: (message, kind) => progress.toasts.push({ message, kind }),
				recordPerfEvent: (kind, message, severity = 'info') =>
					progress.perf.push({ kind, message, severity }),
				now: () => performance.now(),
				setTimeout: (fn, ms) => setTimeout(fn, ms),
				clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>)
			},
			isAnyDeckPlaying,
			media
		);

		// The third REAL module (production wiring: `audio-context-instrumentation.ts`),
		// on the real `ctx` rather than the resume/suspend `shim`: it only reads
		// `outputLatency`/`getOutputTimestamp()`, never resumes anything, so the
		// real AudioContext is the right object. Its own debounce
		// (`LIVENESS_DEAD_POLLS` consecutive real `LIVENESS_POLL_MS` polls) is
		// what a transient post-resume `outputLatency === 0` reading needs to be
		// absorbed by; a raw per-sample check has no such protection and false
		// positives on exactly that transient (found running this harness against
		// this PR's redesign).
		let outputLivenessVerdict: LivenessVerdict = 'idle';
		const liveness = installOutputLiveness(
			ctx,
			{
				pushToast: (message, kind) => progress.toasts.push({ message, kind }),
				recordPerfEvent: (kind, message, severity) => progress.perf.push({ kind, message, severity }),
				setInterval: (fn, ms) => setInterval(fn, ms),
				clearInterval: (h) => clearInterval(h as ReturnType<typeof setInterval>),
				onSnapshot: (snapshot) => {
					outputLivenessVerdict = snapshot.verdict;
				}
			},
			isAnyDeckPlaying
		);

		// The production master meter, so the worklet-error injection lands on a
		// real shipped node rather than a stand-in.
		//
		// DELIBERATELY UNCAUGHT (Sol P1 BLOCKING, PR #1644). This used to swallow
		// an arm failure and record a note, which silently disarmed the
		// worklet-crash event while the soak went on to pass. That is precisely
		// the defect this suite's own docstring warns about: a check that is
		// satisfied without being satisfied. A rejection here propagates to
		// `progress.error` and the spec fails naming it, so "the meter did not
		// arm" is a red soak rather than a quieter green one.
		await attachMeterTaps(ctx, [createMasterMeterSource(masterGain)]);

		// ---- the hostile schedule -------------------------------------------
		const setDevice = (name: string): void => {
			currentDevice = name;
			applyGate();
			media.fire();
		};

		/**
		 * THE INCIDENT, replayed: A -> B -> A with the second edge landing well
		 * inside the cooldown. Pre-fix that edge is discarded and the graph is
		 * stranded on B, which is gone.
		 */
		const flapInsideCooldown = async (): Promise<void> => {
			note('device flap A->B->A, second edge 1.9s in (the 17:44Z incident)');
			setDevice('bluetooth');
			await sleep(1_900);
			setDevice('built-in');
		};

		const burstInsideCooldown = async (): Promise<void> => {
			note('ten device changes inside one cooldown');
			for (let i = 0; i < 10; i += 1) {
				setDevice(i % 2 === 0 ? 'bluetooth' : 'built-in');
				await sleep(180);
			}
			setDevice('built-in');
		};

		const externalSuspend = async (): Promise<void> => {
			note('the OS suspends the context under a playing deck');
			await ctx.suspend();
			for (const h of listeners) h();
			// Left for the shipped watchdog to recover. Its first backoff step is
			// 0ms, so a recoverable suspend costs a sample or two, not a dropout.
			await sleep(1_500);
			if (ctx.state !== 'running') {
				note('the watchdog did not recover the suspend; asking again on a visibility edge');
				noteRecoveryOpportunity('soak: the window became visible');
				await sleep(1_500);
			}
		};

		/**
		 * The shipped `onprocessorerror` HANDLER, invoked. NOT a real processor
		 * crash (Codex P1 BLOCKING, thread 3973440552).
		 *
		 * UNAVAILABLE, and named that way rather than overclaimed: a
		 * `processorerror` cannot be provoked from JavaScript. Only the browser
		 * raises one, when the processor's own constructor or `process()`
		 * throws, and `meter-tap.ts` says so in its own docstring. A synthetic
		 * `dispatchEvent` does not even reach the IDL attribute in Chromium --
		 * `meter-artifact.spec.ts` measured that and pins it. So the real
		 * processor stays healthy here and this event cannot show what an actual
		 * crash does to audio.
		 *
		 * What it DOES exercise, which is real and is all that is claimed: the
		 * handler the shipped `attachMeterTaps` installed on the real node runs,
		 * takes the context-scoped `markMetersUnavailable` path, and does not
		 * interrupt the signal reaching the output. A meter failing must not
		 * take the audio with it, and that is a live path in the soak's own
		 * continuity measurement.
		 */
		const workletError = async (): Promise<void> => {
			note(
				"the master meter's shipped onprocessorerror handler runs " +
					'(a real processor crash is UNAVAILABLE: only the browser can raise one)'
			);
			const node = masterMeterNode();
			if (node === null) {
				throw new Error(
					'the master meter tap is gone, so the worklet-crash event fired against ' +
						'nothing; a soak that cannot inject this event must not report a verdict'
				);
			}
			// NOT optional-chained (Sol P1 BLOCKING, PR #1644). `?.` would skip the
			// whole injection when no handler is installed, after the event was
			// already logged as having happened, so a soak could report a verdict
			// on a hostile event that exercised nothing.
			const handler = node.onprocessorerror;
			if (typeof handler !== 'function') {
				throw new Error(
					'the shipped attachMeterTaps installed no onprocessorerror handler, so the ' +
						'worklet-crash event cannot be injected and this soak has no verdict on it'
				);
			}
			handler.call(node, new ErrorEvent('processorerror'));
			await sleep(500);
		};

		const idleTransition = async (): Promise<void> => {
			note('operator pauses, device changes while paused, then plays again');
			playing = false;
			await sleep(1_200);
			setDevice('bluetooth');
			await sleep(1_200);
			setDevice('built-in');
			playing = true;
		};

		const flapAcrossPause = async (): Promise<void> => {
			// AUDIOLIVE-03's shape: accepted while playing, paused during the
			// window, and the held request must still run.
			note('device change while playing, then a pause inside the debounce');
			setDevice('bluetooth');
			setTimeout(() => {
				playing = false;
			}, 200);
			await sleep(1_000);
			playing = true;
			await sleep(500);
			setDevice('built-in');
		};

		/**
		 * AUDIOLIVE-06's shape for the WATCHDOG, the third instance of #1619's
		 * defect class (Codex P1 BLOCKING, thread 3973882771).
		 *
		 * A recovery edge accepted while a deck was playing, HELD because a
		 * bounded schedule was still running, and the operator pausing before
		 * that schedule finishes. Pausing is the normal reaction to audio
		 * stopping, and re-gating the held edge on playback used to discard it.
		 *
		 * The timings are read off `CONTEXT_RESUME_BACKOFF_MS`, which is
		 * cumulative [0, 150, 550, 1550, 4050, 9050]: the edge lands at 500ms
		 * (inside the schedule), the pause at 1s (before it exits), the device
		 * comes back at 8s (still inside it), and the schedule exits at 9,050ms
		 * -- which is the only moment the held edge can run.
		 *
		 * Pre-fix this event ends with a suspended context and playback resumed,
		 * so the soak's own silence probe ALSO goes red once the deck says it is
		 * playing again. The explicit throw is the sharper of the two signals and
		 * names the cause rather than the symptom.
		 */
		const recoveryEdgeAcrossPause = async (): Promise<void> => {
			note('context drops, a recovery edge lands mid-schedule, then the operator pauses');
			const perArming = CONTEXT_RESUME_BACKOFF_MS.length;
			deviceGone = true;
			resumeAttemptsWhileGone = 0;
			// Two attempts INTO the re-armed schedule, so recovery is out of the
			// first schedule's reach by construction rather than by timing.
			returnDeviceAfterAttempt = perArming + 2;
			await ctx.suspend();
			for (const h of listeners) h();
			await sleep(500);
			// Accepted: a deck IS playing. Held: a schedule is already running.
			noteRecoveryOpportunity('soak: the output device list changed');
			await sleep(500);
			note('the operator pauses while the edge is still held');
			playing = false;
			// Past the first schedule's 9,050ms exit AND past the re-armed
			// schedule's first attempts, with room for a loaded machine.
			await sleep(13_000);
			playing = true;
			await sleep(1_500);
			returnDeviceAfterAttempt = null;
			// The COUNT is the assertion, and it is timing-free: the first
			// schedule can make exactly `perArming` attempts, so anything beyond
			// that number is the held edge and nothing else.
			if (resumeAttemptsWhileGone <= perArming) {
				throw new Error(
					`only ${resumeAttemptsWhileGone} resume attempt(s) were made against the ` +
						`missing device, which is one arming of ${perArming}: the recovery edge ` +
						'accepted while a deck was playing was DISCARDED when the operator paused, ' +
						'so no schedule was ever re-armed'
				);
			}
			if (ctx.state !== 'running') {
				throw new Error(
					'a recovery edge accepted while a deck was playing did not bring the context ' +
						`back: it is ${ctx.state} with playback resumed and the device present ` +
						`after ${resumeAttemptsWhileGone} attempts. Nothing else will ask again - ` +
						'statechange fires on transitions and this context never changed state'
				);
			}
		};

		const schedule: { name: string; run: () => Promise<void> }[] = [
			{ name: 'flapInsideCooldown', run: flapInsideCooldown },
			{ name: 'recoveryEdgeAcrossPause', run: recoveryEdgeAcrossPause },
			{ name: 'externalSuspend', run: externalSuspend },
			{ name: 'burstInsideCooldown', run: burstInsideCooldown },
			{ name: 'workletError', run: workletError },
			{ name: 'idleTransition', run: idleTransition },
			{ name: 'flapAcrossPause', run: flapAcrossPause }
		];
		scheduleNames = schedule.map((entry) => entry.name);

		// ---- the probe -------------------------------------------------------
		const scratch = new Float32Array(analyser.fftSize);
		const masterScratch = new Float32Array(masterAnalyser.fftSize);
		const sampler = setInterval(() => {
			analyser.getFloatTimeDomainData(scratch);
			let sum = 0;
			for (let i = 0; i < scratch.length; i += 1) sum += scratch[i] * scratch[i];
			const rms = Math.sqrt(sum / scratch.length);
			const tMs = performance.now() - t0;
			progress.elapsedMs = tMs;
			progress.samples += 1;
			// Checked HERE rather than in the event loop, which spends most of its
			// life inside a 30s sleep and would strand the device up to 30s late --
			// long enough for the control's own window to close before it began.
			if (options.strandDeviceAtMs !== undefined && !stranded && tMs >= options.strandDeviceAtMs) {
				note('NEGATIVE CONTROL: the device is stranded and no rebind can re-open it');
				stranded = true;
				applyGate();
			}
			if (Number.isFinite(rms) && rms > progress.peakRms) progress.peakRms = rms;
			// Silence only counts while a deck says it is playing. A paused deck
			// is silent on purpose and is not a dropout, which is the same gate
			// the shipped silence watchdog applies.
			if (!playing || (Number.isFinite(rms) && rms >= SILENCE_RMS_FLOOR)) {
				progress.silentSinceMs = null;
			} else {
				if (progress.silentSinceMs === null) progress.silentSinceMs = tMs;
				const runMs = tMs - progress.silentSinceMs;
				if (runMs > progress.longestSilentWhilePlayingMs) {
					progress.longestSilentWhilePlayingMs = runMs;
					progress.longestSilentStartedAtMs = progress.silentSinceMs;
				}
			}

			// ---- device-level liveness (#1642), the SHIPPED decision fold -----
			masterAnalyser.getFloatTimeDomainData(masterScratch);
			let masterSum = 0;
			for (let i = 0; i < masterScratch.length; i += 1) masterSum += masterScratch[i] * masterScratch[i];
			const masterRmsRaw = Math.sqrt(masterSum / masterScratch.length);
			const masterRms = Number.isFinite(masterRmsRaw) ? masterRmsRaw : 0;
			if (masterRms > progress.peakUpstreamRms) progress.peakUpstreamRms = masterRms;
			// REAL, not modelled (see module docstring): the shipped, DEBOUNCED
			// `installOutputLiveness` verdict, the same one
			// `master-silence-report.ts` reads via `audioOutputHealth.snapshot` in
			// production. Playwright cannot make the browser's actual output
			// device report 0 for the debounce's full window, so this stays a
			// live, non-triggering reading for the whole run.
			const outputLatencyDead =
				outputLivenessVerdict === 'dead' || outputLivenessVerdict === 'dead-escalated';
			deviceLivenessState = foldDeviceLivenessSample(deviceLivenessState, {
				playing,
				masterRms,
				outputLatencyDead,
				tMs
			});
			if (deviceLivenessState.verdict === 'device-unreachable') progress.deviceUnreachableVerdicts += 1;
			const deviceUnreachableNow = playing && outputLatencyDead && masterRms >= SILENCE_RMS_FLOOR;
			if (!deviceUnreachableNow) {
				progress.deviceUnreachableSinceMs = null;
				return;
			}
			if (progress.deviceUnreachableSinceMs === null) progress.deviceUnreachableSinceMs = tMs;
			const unreachableRunMs = tMs - progress.deviceUnreachableSinceMs;
			if (unreachableRunMs > progress.longestDeviceUnreachableMs) {
				progress.longestDeviceUnreachableMs = unreachableRunMs;
				progress.deviceUnreachableStartedAtMs = progress.deviceUnreachableSinceMs;
			}
		}, samplePeriodMs);

		teardown = async (): Promise<void> => {
			clearInterval(sampler);
			rebind.uninstall();
			liveness.uninstall();
			detachWatchdog();
			releaseMasterMeterTap();
			teardownMeterTaps();
			try {
				source.stop();
			} catch {
				// Already stopped by a closed context; not a soak result.
			}
			await ctx.close();
		};

		// Let the graph settle and prove signal BEFORE any hostile event, so a
		// run that never had sound fails on `peakRms` rather than looking like a
		// recovery failure.
		await sleep(1_000);

		let step = 0;
		// ONE FULL PASS IS THE FLOOR, whatever duration was asked for (Sol P1
		// BLOCKING, PR #1644). Events are three cooldowns apart, so a short run
		// used to stop after two of seven while the suite asserted only that
		// SOME event had fired -- a verdict on five paths that never ran,
		// including the worklet handler and the two pause cases. The duration is
		// now a floor on SOAKING, not a budget the schedule can be truncated to
		// fit; `SOAK_OVERRUN_SLACK_MS` in the config is what pays for the
		// overrun a short run costs.
		//
		// The stranded arm is deliberately outside that clause: the negative
		// control strands the device inside its first gap and must keep sleeping
		// out its duration rather than spinning on a pass it can never finish.
		while (
			!stopRequested &&
			((!stranded && step < schedule.length) || performance.now() - t0 < options.durationMs)
		) {
			if (!stranded) {
				const event = schedule[step % schedule.length];
				progress.eventsRan.push(event.name);
				await event.run();
				step += 1;
			}
			// THE GAP BETWEEN EVENTS MUST EXCEED THE FAILURE THRESHOLD, which is
			// two cooldowns. Measured the hard way: at a 14s gap, a stranded graph
			// was rescued by the NEXT event's suspend/resume 12s later, because a
			// resume re-binds to the current device no matter what stranded it.
			// The soak then could not tell "the deferral worked" from "an
			// unrelated event happened to un-strand it", and the pre-fix mutation
			// stayed green. Three cooldowns leaves every event's own recovery to
			// be measured, and to run out, before the next one can mask it.
			await sleep(REBIND_COOLDOWN_MS * 3);
		}
	}

	window.__audioSoakHarness = {
		get scheduleNames(): readonly string[] {
			return scheduleNames;
		},
		start(options: SoakOptions): void {
			void run(options)
				.catch((error: unknown) => {
					progress.error = String(error);
				})
				.finally(() => {
					progress.done = true;
				});
		},
		progress: () => JSON.parse(JSON.stringify(progress)) as SoakProgress,
		stop: async (): Promise<void> => {
			stopRequested = true;
			if (teardown !== null) await teardown();
		},
		constants: {
			SILENCE_RMS_FLOOR,
			REBIND_COOLDOWN_MS,
			REBIND_DEBOUNCE_MS,
			CONTEXT_RESUME_BACKOFF_MS
		}
	};
}

install();
