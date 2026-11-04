/**
 * Audio I/O device check, the Web Audio half: plays the test beeps and opens
 * the microphone for the input meter. HeadphoneCluster loads it on demand, so
 * the initial page load carries only the pure helpers in `io-device-test.ts`.
 */
import {
	audioContextSinkIdIsSupported,
	browserSinkId,
	landedMasterSinkId,
	liveCalibrationGraph,
	mainReopenFailed,
	nativeCueSinkAvailable,
	onOutputMoveStarted,
	outputSwitchesPending,
	splitCableGains,
	withHeadphoneOperationTimeout
} from './headphones';
import { sameOutputDevice } from './main-cue-collision';
import { mixerState } from './state.svelte';
import { recordPerfEvent } from '$lib/rb/perf-event-log';
import { micOpenRefusal, NO_INPUT_PICKED, rmsOf, scheduleIoTestBeeps, type IoTestBus } from './io-device-test';

/** Close a short-lived test context. Bounded, so a close that never settles
 * (the device vanished, the audio service died) cannot keep the Test busy;
 * a failure or timeout is recorded rather than thrown, since the Test itself
 * already ran. */
function closeQuietly(ctx: AudioContext): Promise<void> {
	if (ctx.state === 'closed') return Promise.resolve();
	return withHeadphoneOperationTimeout('test context close', ctx.close()).catch((error: unknown) => {
		recordPerfEvent('io-test-context-close-failed', error instanceof Error ? error.message : String(error), null, 'warn');
	});
}

/** How long the input meter waits for the microphone, permission prompt
 * included: long enough to read and answer the prompt. */
export const INPUT_METER_OPEN_TIMEOUT_MS = 30_000;
/** Why an open AUDIO IN watch stopped on its own: the microphone went away. */
export const INPUT_ENDED = 'The picked microphone stopped sending audio (unplugged?), so AUDIO IN was not measured. Check it, then test again.';

type BeepTarget = { ctx: AudioContext; node: AudioNode; closeAfter: boolean };

let playing: { osc: OscillatorNode; gain: GainNode; closeAfter: boolean; ctx: AudioContext; finish: () => void } | null = null;
let beepGeneration = 0;
/** Aborts the setup of the Test still opening its short-lived output (a
 * setSinkId or resume that has not settled), so a cut never waits on it. */
let beepSetup: AbortController | null = null;

/** Why an aborted open failed: the signal's own reason when it gave one. */
function abortReason(signal: AbortSignal): Error {
	return signal.reason instanceof Error ? signal.reason : new Error('The audio open was cancelled.');
}

/** `work`, or a rejection the moment `signal` aborts, whichever comes first. */
function untilAborted<T>(signal: AbortSignal, work: Promise<T>): Promise<T> {
	if (signal.aborted) return Promise.reject(abortReason(signal));
	return new Promise<T>((resolve, reject) => {
		const onAbort = (): void => reject(abortReason(signal));
		signal.addEventListener('abort', onAbort, { once: true });
		work.then(
			(value) => {
				signal.removeEventListener('abort', onAbort);
				resolve(value);
			},
			(error: unknown) => {
				signal.removeEventListener('abort', onAbort);
				reject(error);
			}
		);
	});
}

/** How long a cut beep fades before its nodes are released. */
const CUT_FADE_SEC = 0.03;

function stopPlaying(): void {
	const current = playing;
	playing = null;
	if (current === null) return;
	// Released once the fade has played out: disconnecting at once would cut
	// the beep off the output mid-fade, the click the fade exists to avoid.
	// `ended` never fires on a closed or stalled context, so a timer backs it.
	// The Test ends only then, so its owner (the dispatcher's headphone scope)
	// is held until the tone is really gone.
	let released = false;
	const release = (): void => {
		if (released) return;
		released = true;
		clearTimeout(fallback);
		// A bare disconnect() does not throw for a node with no connections, so a
		// throw here is unexpected: recorded, never hidden.
		try {
			current.osc.disconnect();
			current.gain.disconnect();
		} catch (error) {
			recordPerfEvent('io-test-beep-disconnect-failed', error instanceof Error ? error.message : String(error), null, 'warn');
		}
		if (current.closeAfter) void closeQuietly(current.ctx);
		current.finish();
	};
	const fallback = setTimeout(release, (CUT_FADE_SEC + 0.2) * 1000);
	current.osc.onended = release;
	try {
		// Fade the cut beep out instead of stopping it mid-cycle, which clicks.
		const now = current.ctx.currentTime;
		current.gain.gain.cancelScheduledValues(now);
		current.gain.gain.setTargetAtTime(0, now, 0.005);
		current.osc.stop(now + CUT_FADE_SEC);
	} catch (error) {
		// stop() on an oscillator already stopped throws InvalidStateError; any
		// other failure is recorded.
		if (!(error instanceof Error && error.name === 'InvalidStateError')) {
			recordPerfEvent('io-test-beep-cut-failed', error instanceof Error ? error.message : String(error), null, 'warn');
		}
		release();
	}
}

type LiveBeepNodes = { bridgeSender: unknown; bridgeInput: AudioNode; cueSum: AudioNode; splitLeftGain: AudioNode };

/**
 * Where a test beep enters the live graph. Split L/R carries MAIN on the left
 * channel and cue on the right, so MAIN (and AUDIO IN, which beeps on MAIN)
 * enters the left half and CUE enters cueSum; neither may reach the raw
 * destination, which would upmix the beep into both ears. Outside split, CUE
 * uses the two-output bridge when one is connected.
 */
export function liveBeepNode(bus: IoTestBus, ctx: BaseAudioContext, nodes: LiveBeepNodes, outputMode: string): AudioNode {
	const split = outputMode === 'split_cable';
	if (bus === 'cue') return nodes.bridgeSender !== null && !split ? nodes.bridgeInput : nodes.cueSum;
	return split ? nodes.splitLeftGain : ctx.destination;
}

type LiveCueSink = {
	bridgeSender: unknown;
	cueContext: { state: string } | null;
	nativeCue?: unknown;
};

/**
 * Whether a CUE Test beep sent into the live graph can reach the headphones.
 * Split L/R plays CUE on the room context itself. Two outputs reach the CUE
 * device only through the bridge (the native cue reads it too), and its
 * receiver can be stopped while the menu still shows the CUE: a failed pick
 * and rollback leave it suspended, and a failed bridge worklet tears the
 * bridge down. A beep sent then finishes on MAIN's clock and sounds nowhere.
 */
export function liveCueSinkOpen(nodes: LiveCueSink, outputMode: string, active: boolean): boolean {
	if (outputMode === 'split_cable') return true;
	if (nodes.bridgeSender === null) return outputMode !== 'two_outputs';
	if (!active) return false;
	if (nodes.nativeCue !== null && nodes.nativeCue !== undefined) return true;
	return nodes.cueContext?.state === 'running';
}

type LiveRouteState = {
	output_mode: string;
	outputs: readonly { id: string; label: string }[];
	routes: { master: { state: string }; cue: { state: string } };
	/** The two-output CUE's liveness verdict (headphone-output-liveness). */
	liveness_verdict?: string;
	/** Headphone MIX and LEVEL: a split CUE beep enters ahead of both. */
	mix?: number;
	level?: number;
};

/** Liveness verdicts for a CUE output that runs but makes no sound. */
const SILENT_CUE_VERDICTS = new Set(['stalled', 'dead', 'dead-escalated']);

/**
 * Why a Test must not beep the live graph: it plays wherever the graph is
 * routed, so a shown device the graph is not on would be reported as tested
 * while another one sounded. MAIN (and AUDIO IN, and CUE in split L/R, which
 * all beep on MAIN) is refused when its route failed or its sink last landed
 * on another device, or has not landed at all while a device is shown (a route
 * remount forgets the landing but keeps the pick); two-output CUE when its route failed or its sink is on
 * another device. Null when the graph is on the device shown.
 */
/**
 * Why a Test must wait for an output switch: while the bus's sink is still
 * moving, the live graph plays on the previous device while the menu shows
 * the new one, so a beep would be reported as testing the wrong device (and a
 * CUE beep could reach the room). MAIN, AUDIO IN and split CUE follow MAIN's
 * move; two-output CUE follows the CUE pick. Null when nothing is switching.
 */
export function outputSwitchRefusal(
	bus: IoTestBus,
	outputMode: string,
	pending: { master: boolean; cue: boolean }
): string | null {
	const onMain = bus !== 'cue' || outputMode === 'split_cable';
	if (onMain) return pending.master ? 'MAIN is still switching to the picked device. Wait for it, then test.' : null;
	return pending.cue ? 'Headphone output is still switching to the picked device. Wait for it, then test.' : null;
}

/** Whether an output move cuts a Test on `bus`: every move does, except a
 * recovery of the two-output CUE alone (`only` 'cue'), which leaves a Test on
 * MAIN sounding. In split L/R the CUE Test plays on MAIN, so it stays too. */
export function outputMoveCutsTest(only: 'cue' | undefined, bus: IoTestBus, outputMode: string): boolean {
	if (only !== 'cue') return true;
	return bus === 'cue' && outputMode !== 'split_cable';
}

export function liveRouteRefusal(
	bus: IoTestBus,
	deviceId: string | null,
	hp: LiveRouteState,
	landedMaster: string | null,
	cueDeviceId: string | null,
	/** The live room's last reopen after a MAIN change failed (mainReopenFailed). */
	reopenFailed = false
): string | null {
	const onMain = bus !== 'cue' || hp.output_mode === 'split_cable';
	if (onMain) {
		if (hp.routes.master.state === 'failed') return 'MAIN did not open on the picked device. Pick MAIN again, then test.';
		if (reopenFailed) return 'MAIN audio did not reopen after the output change, so it may be silent. Pick MAIN again, then test.';
		// A split CUE beep enters cueSum, ahead of MIX and LEVEL, so a zero cue
		// gain would end it on time and pass it unheard.
		if (bus === 'cue' && hp.output_mode === 'split_cable' && hp.mix !== undefined && hp.level !== undefined && splitCableGains('split_cable', hp.mix, hp.level).rightCue <= 0) {
			return 'Headphone MIX or LEVEL is all the way down, so the Test would be silent. Turn LEVEL up and MIX toward CUE, then test.';
		}
		if (deviceId === null || deviceId === '' || deviceId === 'default') return null;
		// A route teardown forgets the landing while the menu keeps its pick, so
		// an unknown landing may be the OS default: unverified, not a match.
		if (landedMaster === null) return 'MAIN has not landed on the picked device since audio started. Pick MAIN again, then test.';
		if (!sameOutputDevice(hp.outputs, landedMaster, deviceId)) {
			return 'MAIN is still playing on another device than the one picked. Pick MAIN again, then test.';
		}
		return null;
	}
	if (hp.routes.cue.state === 'failed') return 'Headphone output did not open on the picked device. Pick CUE again, then test.';
	// Running but known silent: a beep would end on time and pass unheard.
	if (SILENT_CUE_VERDICTS.has(hp.liveness_verdict ?? '')) {
		return 'Headphone output is not producing sound. Pick CUE again, then test.';
	}
	// No CUE landing with a device shown: the sink never opened (a restore or
	// pick that failed before it landed), so a beep would sound nowhere.
	if (cueDeviceId === null && deviceId !== null && deviceId !== '') {
		return 'Headphone output has not opened on the picked device. Pick CUE again, then test.';
	}
	if (cueDeviceId !== null && deviceId !== null && !sameOutputDevice(hp.outputs, cueDeviceId, deviceId)) {
		return 'Headphone output is still on another device than the one picked. Pick CUE again, then test.';
	}
	return null;
}

async function beepTarget(bus: IoTestBus, deviceId: string | null, signal: AbortSignal): Promise<BeepTarget> {
	// No CUE device picked (One Output, or never set): refuse before the live
	// graph, whose bridge can outlive the pick and would beep the old headphones.
	if (bus === 'cue' && (deviceId === null || deviceId === '')) {
		throw new Error('No headphone output is picked. Pick the CUE device, then test again.');
	}
	const hp = mixerState.headphones;
	// A switch still pending refuses with or without a live graph: before any
	// deck builds one, a device change's MAIN move may still be landing, and a
	// short-lived context would beep wherever the room was before it.
	const switching = outputSwitchRefusal(bus, hp.output_mode, outputSwitchesPending());
	if (switching !== null) throw new Error(switching);
	const live = liveCalibrationGraph();
	if (live.ctx !== null && live.nodes !== null) {
		if (bus === 'cue' && !liveCueSinkOpen(live.nodes, hp.output_mode, hp.active)) {
			throw new Error('Headphone output is not open yet. Pick the device and start audio, then test again.');
		}
		const refusal = liveRouteRefusal(bus, deviceId, hp, landedMasterSinkId(), live.nodes.cueDeviceId, mainReopenFailed());
		if (refusal !== null) throw new Error(refusal);
		return { ctx: live.ctx, node: liveBeepNode(bus, live.ctx, live.nodes, mixerState.headphones.output_mode), closeAfter: false };
	}
	if (bus === 'cue' && (deviceId === null || deviceId.startsWith('native:') || typeof AudioContext === 'undefined' || !audioContextSinkIdIsSupported(AudioContext.prototype))) {
		throw new Error('Headphone output is not open yet. Pick the device and start audio, then test again.');
	}
	// The Mac shell pins MAIN by making it the OS default, which a short-lived
	// context follows. With no landing known (a remount cleared it and the
	// shell's pin), the default may be another device than the one shown.
	if (bus !== 'cue' && deviceId !== null && deviceId.startsWith('native:')) {
		const refusal = liveRouteRefusal(bus, deviceId, hp, landedMasterSinkId(), null);
		if (refusal !== null) throw new Error(refusal);
	}
	const ctx = new AudioContext();
	try {
		if (
			deviceId !== null &&
			deviceId !== '' &&
			!deviceId.startsWith('native:') &&
			audioContextSinkIdIsSupported(ctx)
		) {
			const sinkable = ctx as AudioContext & { setSinkId(id: string): Promise<void> };
			await untilAborted(signal, withHeadphoneOperationTimeout('test setSinkId', sinkable.setSinkId(browserSinkId(deviceId))));
		}
		if (ctx.state === 'suspended') await untilAborted(signal, withHeadphoneOperationTimeout('test resume', ctx.resume()));
	} catch (error) {
		// A cut Test returns at once; its context still closes, on its own.
		if (signal.aborted) void closeQuietly(ctx);
		else await closeQuietly(ctx);
		throw error;
	}
	return { ctx, node: ctx.destination, closeAfter: true };
}

/** Cut any test beep still sounding (the panel closed). */
export function stopIoTestBeeps(): void {
	beepGeneration += 1;
	beepSetup?.abort(new Error('The Test was cut.'));
	beepSetup = null;
	stopPlaying();
}

/**
 * Play the beep pattern on `bus`. A later call cuts the one still sounding.
 * `onStart` gets the `performance.now()` time the first beep sounds, so the
 * meter only shows the tone once it is actually scheduled.
 */
export async function playIoTestBeeps(
	bus: IoTestBus,
	deviceId: string | null,
	onStart: (startsAtMs: number) => void = () => {},
	signal?: AbortSignal
): Promise<boolean> {
	// An aborted caller plays nothing; one aborted mid-Test cuts its own beep
	// (never a later Test's), so a replacement never waits on its tone.
	// Answers whether the whole pattern played: false for a Test cut by
	// anyone (a new Test, a close, a teardown) before it finished.
	if (signal?.aborted) return false;
	const played = playBeeps(bus, deviceId, onStart);
	if (signal === undefined) return played;
	const generation = beepGeneration;
	const onAbort = (): void => {
		if (generation === beepGeneration) stopIoTestBeeps();
	};
	signal.addEventListener('abort', onAbort, { once: true });
	try {
		return await played;
	} finally {
		signal.removeEventListener('abort', onAbort);
	}
}

async function playBeeps(bus: IoTestBus, deviceId: string | null, onStart: (startsAtMs: number) => void): Promise<boolean> {
	const generation = ++beepGeneration;
	stopPlaying();
	beepSetup?.abort(new Error('The Test was cut.'));
	const setup = new AbortController();
	beepSetup = setup;
	// A sink move started on its own (a device change's re-pin, a correction)
	// runs outside the dispatcher's scope: it cuts this Test, which would
	// otherwise start on one device and finish on another, or into nothing.
	let mine: { osc: OscillatorNode } | null = null;
	const stopMoving = onOutputMoveStarted((only) => {
		if (generation !== beepGeneration) return;
		if (!outputMoveCutsTest(only, bus, mixerState.headphones.output_mode)) return;
		setup.abort(new Error('The output switched during the Test.'));
		if (mine !== null && playing === mine) stopPlaying();
	});
	try {
		return await playBeepsOn(bus, deviceId, onStart, generation, setup, (current) => (mine = current));
	} finally {
		stopMoving();
	}
}

async function playBeepsOn(
	bus: IoTestBus,
	deviceId: string | null,
	onStart: (startsAtMs: number) => void,
	generation: number,
	setup: AbortController,
	started: (current: { osc: OscillatorNode }) => void
): Promise<boolean> {
	let target: BeepTarget;
	try {
		target = await beepTarget(bus, deviceId, setup.signal);
	} catch (error) {
		// Cut while its output was still opening: ends like any cut Test.
		if (beepSetup === setup) beepSetup = null;
		if (setup.signal.aborted) return false;
		throw error;
	}
	if (setup.signal.aborted) {
		// Its output switched as it opened: plays nothing.
		if (beepSetup === setup) beepSetup = null;
		if (target.closeAfter) void closeQuietly(target.ctx);
		return false;
	}
	if (generation !== beepGeneration) {
		// Cut as its output opened: returns at once, the close runs on its own.
		if (target.closeAfter) void closeQuietly(target.ctx);
		return false;
	}
	const osc = target.ctx.createOscillator();
	const gain = target.ctx.createGain();
	gain.gain.value = 0;
	osc.connect(gain);
	gain.connect(target.node);
	let finish = (): void => {};
	const done = new Promise<void>((resolve) => {
		finish = resolve;
	});
	const mine = { osc, gain, closeAfter: target.closeAfter, ctx: target.ctx, finish };
	playing = mine;
	started(mine);
	let playedOut = false;
	osc.onended = () => {
		playedOut = true;
		finish();
	};
	let fallback: ReturnType<typeof setTimeout> | undefined;
	try {
		// A live graph still resuming is raced against a cut too, so the cut
		// Test never waits out the resume.
		if (target.ctx.state === 'suspended') await untilAborted(setup.signal, withHeadphoneOperationTimeout('test resume', target.ctx.resume()));
		const start = target.ctx.currentTime + 0.03;
		const end = scheduleIoTestBeeps(osc, gain.gain, start);
		osc.start(start);
		osc.stop(end + 0.02);
		// `ended` never fires if the context is closed or stays suspended (a
		// device switch rebuilds the graph mid-test), so the test also ends on
		// a timer instead of hanging. Armed only once the tones are scheduled,
		// so a slow resume can never end the Test before it starts.
		fallback = setTimeout(() => finish(), (end + 0.02 - target.ctx.currentTime + 1) * 1000);
		onStart(performance.now() + (start - target.ctx.currentTime) * 1000);
		await done;
		// Played only when the tone itself ended: a cut releases the Test
		// (`playing` moves off it) and a stalled output ends on the timer.
		return playing === mine && playedOut;
	} catch (error) {
		// Cut while the live graph resumed: the cut already released it.
		if (setup.signal.aborted) return false;
		throw error;
	} finally {
		if (beepSetup === setup) beepSetup = null;
		clearTimeout(fallback);
		if (playing === mine) {
			playing = null;
			try {
				osc.disconnect();
				gain.disconnect();
			} catch (error) {
				recordPerfEvent('io-test-beep-disconnect-failed', error instanceof Error ? error.message : String(error), null, 'warn');
			}
			if (target.closeAfter) await closeQuietly(target.ctx);
		}
	}
}

/** CUEOUT-8393 / AUDFID-04: the one gate every microphone open in the Mac app
 * passes before `getUserMedia` (the AUDIO IN Test, the agent `io_device_test`
 * and the cue alignment calibration). In the Mac app a mic opened while MAIN
 * plays can switch a Bluetooth MAIN to the SCO profile and silence it. Throws
 * the refusal; returns when the open is allowed. */
export function assertMicOpenAllowedNow(): void {
	const roomSignal = mixerState.headphones.signals.master;
	const refusal = micOpenRefusal({
		macAppWebview: nativeCueSinkAvailable(),
		masterRms: roomSignal.state === 'measured' ? roomSignal.rms : null
	});
	if (refusal !== null) throw new Error(refusal);
}

/** Live mic level for the input meter. Caller stops it by calling the return.
 * An abort of `signal` while the microphone is still opening (a permission
 * prompt left up) fails the open at once, and a grant that lands later is
 * stopped rather than left with nobody to stop it. A microphone that stops
 * sending audio once open (unplugged, revoked) stops the watch and calls
 * `onEnded` with the reason, so nobody reads its silence as a level. */
export async function startInputLevelWatch(
	deviceId: string | null,
	onRms: (rms: number) => void,
	signal?: AbortSignal,
	onEnded?: (reason: string) => void
): Promise<() => void> {
	if (deviceId === null || deviceId === '') throw new Error(NO_INPUT_PICKED);
	if (signal?.aborted) throw abortReason(signal);
	assertMicOpenAllowedNow();
	if (typeof navigator === 'undefined' || navigator.mediaDevices?.getUserMedia === undefined) {
		throw new Error('This app cannot open microphones here, so AUDIO IN cannot be measured.');
	}
	const audio: MediaTrackConstraints = {
		echoCancellation: false,
		noiseSuppression: false,
		autoGainControl: false
	};
	if (!deviceId.startsWith('native:')) audio.deviceId = { exact: deviceId };
	// Bounded, so a permission prompt left unanswered (or a device that never
	// opens) fails the watch instead of holding it open forever. A grant that
	// lands after the bound has nobody to stop it, so its tracks stop at once.
	const request = navigator.mediaDevices.getUserMedia({ audio, video: false });
	let stream: MediaStream;
	const opened = signal === undefined ? request : untilAborted(signal, request);
	try {
		stream = await withHeadphoneOperationTimeout('input meter open', opened, INPUT_METER_OPEN_TIMEOUT_MS);
		micOpenedThisSession = true;
	} catch (error) {
		void request.then(
			(late) => {
				for (const track of late.getTracks()) track.stop();
			},
			// The timeout or abort already failed the watch; keep the real cause.
			(late: unknown) => {
				recordPerfEvent('input-meter-open-late-failure', late instanceof Error ? late.message : String(late), null, 'warn');
			}
		);
		throw error;
	}
	const stopTracks = (): void => {
		for (const track of stream.getTracks()) track.stop();
	};
	let ctx: AudioContext | null = null;
	let source: MediaStreamAudioSourceNode;
	let analyser: AnalyserNode;
	try {
		ctx = new AudioContext();
		if (ctx.state === 'suspended') {
			const resumed = withHeadphoneOperationTimeout('input meter resume', ctx.resume());
			await (signal === undefined ? resumed : untilAborted(signal, resumed));
		}
		source = ctx.createMediaStreamSource(stream);
		analyser = ctx.createAnalyser();
		analyser.fftSize = 256;
		source.connect(analyser);
		// Aborted while the meter's context resumed: nothing may stay open.
		if (signal?.aborted) throw abortReason(signal);
	} catch (error) {
		// Never leave the mic open (and its indicator lit) behind a failure.
		stopTracks();
		// Aborted: the caller returns at once; the context closes on its own.
		if (ctx !== null) {
			if (signal?.aborted) void closeQuietly(ctx);
			else await closeQuietly(ctx);
		}
		throw error;
	}
	const liveCtx = ctx;
	const buf = new Float32Array(analyser.fftSize);
	let raf = 0;
	let ended = false;
	let released = false;
	// One teardown for a microphone that ends and for the caller's stop, so a
	// meter whose device ends never keeps its graph while the panel stays open.
	const release = (): void => {
		if (released) return;
		released = true;
		cancelAnimationFrame(raf);
		source.disconnect();
		void closeQuietly(liveCtx);
		stopTracks();
	};
	const end = (): void => {
		if (ended) return;
		ended = true;
		release();
		onRms(0);
		onEnded?.(INPUT_ENDED);
	};
	for (const track of stream.getTracks()) track.onended = end;
	const tick = (): void => {
		if (stream.getTracks().some((track) => track.readyState === 'ended')) return end();
		analyser.getFloatTimeDomainData(buf);
		onRms(rmsOf(buf));
		raf = requestAnimationFrame(tick);
	};
	raf = requestAnimationFrame(tick);
	return () => {
		// A capture that ended before its event arrived is still reported.
		if (stream.getTracks().some((track) => track.readyState === 'ended')) end();
		// A stopped meter reads zero, never its last level: a hands-free input's
		// passive watch does not reopen behind a Test to overwrite it.
		if (!ended) onRms(0);
		ended = true;
		release();
	};
}

/** A microphone this page opened. Only where the permission query cannot
 * answer does it stand in for one: a query that answers is current, so access
 * revoked since then is never read as still granted. */
let micOpenedThisSession = false;

/**
 * Whether the input meter may open the microphone without a prompt. The
 * permission query answers where it can; where it cannot (no query, no
 * `microphone` descriptor, a rejected query) that is recorded, and the answer
 * comes from what the browser does expose without asking: a microphone opened
 * this session, or input devices whose labels are readable, which browsers
 * show only once microphone access is granted.
 */
export async function microphonePermissionGranted(): Promise<boolean> {
	if (typeof navigator === 'undefined') return false;
	if (navigator.permissions?.query !== undefined) {
		try {
			const status = await navigator.permissions.query({ name: 'microphone' as PermissionName });
			return status.state === 'granted';
		} catch (error) {
			recordPerfEvent('mic-permission-query-failed', error instanceof Error ? error.message : String(error), null, 'warn');
		}
	} else {
		recordPerfEvent('mic-permission-query-failed', 'this browser has no permission query', null, 'warn');
	}
	if (micOpenedThisSession) return true;
	try {
		const devices = (await navigator.mediaDevices?.enumerateDevices?.()) ?? [];
		return devices.some((device) => device.kind === 'audioinput' && device.label !== '');
	} catch (error) {
		recordPerfEvent('mic-permission-query-failed', error instanceof Error ? error.message : String(error), null, 'warn');
		return false;
	}
}
