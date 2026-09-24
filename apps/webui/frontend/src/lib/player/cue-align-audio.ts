/**
 * CUEOUT-14: the audio half of the calibration effects, split from headphones.ts.
 * Chirp playback, the shared-clock mic capture, and the live-graph binding the
 * calibration controller runs against. Loaded on demand with the controller by
 * rb/cue-align-session.svelte.ts, so none of it is in the initial load of "/":
 * nothing here runs until someone starts a calibration.
 */

import { calibrationBlockers } from '$lib/player/cue-align-policy';
import type {
	AppliedAlignment,
	CueAlignBus,
	CueAlignChirpPurpose,
	CueAlignEffects,
	MicHandle,
	OpenCapture
} from '$lib/player/cue-align.svelte';
import {
	applyUnsavedAlignmentDelays,
	liveCalibrationGraph,
	masterDelayNode,
	requireHeadphoneDeviceApi,
	setHeadDelayMs,
	setMasterDelayMs,
	unlockAudioInputConstraints,
	withHeadphoneOperationTimeout,
	type HeadphoneNodes
} from '$lib/player/headphones';
import { persistMixerConfig } from '$lib/player/mixer-config';
import { mixerState } from '$lib/player/state.svelte';

/** Which node a calibration chirp is injected at, for the given bus and purpose. This is
 * the actual routing decision `_cueAlignChirpTarget` resolves to a node from, not a
 * separate description of it, so a regression back to the measure-only bypass points
 * (`destination`, `bridgeInput`) for `verify` is caught by asserting on this function. */
export function cueAlignChirpTargetKey(
	bus: CueAlignBus,
	purpose: CueAlignChirpPurpose = 'measure'
): 'destination' | 'bridgeInput' | 'masterDelayInput' | 'headDelayInput' {
	if (purpose === 'verify') {
		return bus === 'master' ? 'masterDelayInput' : 'headDelayInput';
	}
	return bus === 'master' ? 'destination' : 'bridgeInput';
}

/** Routes on `cueAlignChirpTargetKey` rather than re-deriving the bus/purpose split, so the
 * key the regression test asserts on is the SAME decision the real chirp is sent to, not a
 * second copy of it that could silently drift out of sync. */
function _cueAlignChirpTarget(
	bus: CueAlignBus,
	purpose: CueAlignChirpPurpose,
	nodes: HeadphoneNodes,
	ctx: AudioContext
): AudioNode {
	const key = cueAlignChirpTargetKey(bus, purpose);
	// Verification chirps enter each delay line directly: through the delay, so
	// the plan is what gets tested, and past every user gain (master mute, MIX,
	// LEVEL), so a valid plan cannot fail just because a knob is at zero.
	if (key === 'masterDelayInput') {
		const roomDelay = masterDelayNode();
		if (roomDelay === null) {
			throw new Error('cue alignment verification needs the room delay node');
		}
		return roomDelay;
	}
	if (key === 'headDelayInput') {
		return nodes.delay;
	}
	// Stage one/two measure raw path latency: master bypasses ROOM delay; cue uses bridgeInput
	// (not the room delay line) as before round 7.
	return bus === 'master' ? ctx.destination : nodes.bridgeInput;
}

/** CUEOUT-14: one chirp train to ONE node, cancellable. The master check
 * targets `ctx.destination` directly so the room delay line is bypassed and
 * the measurement is the raw output-path latency; the cue check targets the
 * cue bridge input on the main context clock.
 *
 * `gain` is the stage-one ladder rung: the calibration raises it until the mic
 * hears the bus, so a quiet cue sink is climbed rather than failed. */
async function _playChirpTrain(
	ctx: AudioContext,
	target: AudioNode,
	samples: Float32Array,
	sampleRate: number,
	signal: AbortSignal,
	gain: number,
	whenSec: number
): Promise<void> {
	signal.throwIfAborted();
	if (!Number.isFinite(gain) || gain <= 0 || gain > 1) {
		throw new RangeError(`cue alignment chirp gain must be in (0, 1], got ${gain}`);
	}
	if (!Number.isFinite(whenSec) || whenSec <= 0) {
		throw new RangeError(`cue alignment chirp start must be a context time, got ${whenSec}`);
	}
	const buffer = ctx.createBuffer(1, samples.length, sampleRate);
	buffer.copyToChannel(new Float32Array(samples), 0);
	const src = ctx.createBufferSource();
	src.buffer = buffer;
	const level = ctx.createGain();
	level.gain.value = gain;
	src.connect(level);
	level.connect(target);
	try {
		await new Promise<void>((resolve, reject) => {
			const onAbort = () => {
				src.onended = null;
				try {
					src.stop();
				} catch {
					// already ended
				}
				reject(signal.reason instanceof Error ? signal.reason : new Error('cue alignment chirp aborted'));
			};
			signal.addEventListener('abort', onAbort, { once: true });
			src.onended = () => {
				signal.removeEventListener('abort', onAbort);
				resolve();
			};
			try {
				src.start(whenSec);
			} catch (error) {
				signal.removeEventListener('abort', onAbort);
				reject(error instanceof Error ? error : new Error(String(error)));
			}
		});
	} finally {
		src.disconnect();
		level.disconnect();
	}
}

function _sleepMs(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Capture `durationMs` of the mic into a Float32Array at `sampleRate`,
 * rejecting (and closing the capture context) the moment `signal` aborts. */
/**
 * Open a capture on the SAME AudioContext the chirp is played from, and
 * resolve as soon as the microphone is really delivering frames, reporting the
 * context time of the first one.
 *
 * Both halves of that are load-bearing, and both replace something that was
 * measured failing on real hardware Wed 16 Sep 2026:
 *
 * - A capture on its own fresh `new AudioContext()` shares no clock with the
 *   chirp, so a captured position cannot be turned into a latency. The same
 *   bus, heard clearly at peak 0.48 four times in a row, reported 174, 112,
 *   121 and 203 ms.
 * - Returning only when the capture is FULL meant the caller had to guess how
 *   long the context takes to start. It guessed 40 ms; a fresh context on this
 *   Mac routinely takes longer, and the chirp then played into a capture that
 *   had not begun.
 */
function _openChirpCapture(
	ctx: AudioContext,
	src: MediaStreamAudioSourceNode,
	durationMs: number,
	signal: AbortSignal
): Promise<OpenCapture> {
	signal.throwIfAborted();
	const frames = Math.ceil((durationMs / 1000) * ctx.sampleRate);
	const out = new Float32Array(frames);
	let offset = 0;
	const processor = ctx.createScriptProcessor(2048, 1, 1);
	// The mic must reach a destination for the graph to pull frames at all, so
	// it goes there through a hard zero: nothing of it is ever audible.
	const silent = ctx.createGain();
	silent.gain.value = 0;
	src.connect(processor);
	processor.connect(silent);
	silent.connect(ctx.destination);

	const teardown = (): void => {
		processor.onaudioprocess = null;
		processor.disconnect();
		src.disconnect(processor);
		silent.disconnect();
	};

	return new Promise<OpenCapture>((resolveOpen, rejectOpen) => {
		let settledOpen = false;
		const samples = new Promise<Float32Array>((resolveSamples, rejectSamples) => {
			const timeoutId = setTimeout(() => {
				teardown();
				const error = new Error(`cue alignment record timed out after ${durationMs}ms`);
				if (!settledOpen) { settledOpen = true; rejectOpen(error); }
				rejectSamples(error);
			}, durationMs + 2500);
			const onAbort = (): void => {
				clearTimeout(timeoutId);
				teardown();
				const error = signal.reason instanceof Error ? signal.reason : new Error('cue alignment record aborted');
				if (!settledOpen) { settledOpen = true; rejectOpen(error); }
				rejectSamples(error);
			};
			signal.addEventListener('abort', onAbort, { once: true });
			processor.onaudioprocess = (event: AudioProcessingEvent) => {
				if (!settledOpen) {
					settledOpen = true;
					// The event's own buffer-aligned stamp, not `currentTime`: the
					// main thread can run this callback late, and reading the clock
					// here would turn that scheduling jitter into measured latency.
					// Any fixed offset between this stamp and the first captured
					// frame is the same on both buses and cancels in cue - master.
					if (!Number.isFinite(event.playbackTime)) {
						const error = new Error(`capture event has no playbackTime (${event.playbackTime})`);
						teardown();
						rejectOpen(error);
						return;
					}
					resolveOpen({ startedAtSec: event.playbackTime, samples });
				}
				const input = event.inputBuffer.getChannelData(0);
				const n = Math.min(input.length, frames - offset);
				out.set(input.subarray(0, n), offset);
				offset += n;
				if (offset >= frames) {
					clearTimeout(timeoutId);
					signal.removeEventListener('abort', onAbort);
					teardown();
					resolveSamples(out);
				}
			};
		});
		samples.catch(() => undefined);
	});
}

interface _MicStreamHandle extends MicHandle {
	stream: MediaStream;
	/** ONE source node for the whole run, pulled continuously. */
	source: MediaStreamAudioSourceNode;
}

/**
 * CUEOUT-14: the audio half of the calibration effects, bound to the LIVE
 * headphone graph. Throws (rather than measuring the wrong sinks) unless every
 * precondition holds, and the message names the ones that do not, so a missing
 * audio graph is never reported as a missing device.
 */
export function cueAlignAudioEffects(): Pick<
	CueAlignEffects,
	| 'sampleRate'
	| 'getUserMedia'
	| 'playTrain'
	| 'openCapture'
	| 'contextTimeSec'
	| 'sleep'
	| 'now'
	| 'persist'
	| 'readDelays'
	| 'setDelays'
	| 'alignmentMode'
	| 'deviceIds'
> {
	const { ctx, nodes } = liveCalibrationGraph();
	const cueId = mixerState.headphones.selected_output_device_id;
	const blockers = calibrationBlockers({
		audio_graph_ready: ctx !== null && nodes !== null,
		output_mode: mixerState.headphones.output_mode,
		selected_output_device_id: cueId
	});
	if (blockers.length > 0) {
		throw new Error(`cue alignment calibration cannot start: ${blockers.join('; ')}`);
	}
	if (ctx === null || nodes === null || cueId === null) {
		throw new Error('cue alignment calibration: a precondition is null that calibrationBlockers passed');
	}
	return {
		sampleRate: () => ctx.sampleRate,
		async getUserMedia(): Promise<_MicStreamHandle> {
			const mediaDevices = requireHeadphoneDeviceApi();
			const listed = await withHeadphoneOperationTimeout('enumerateDevices', mediaDevices.enumerateDevices());
			const audio = unlockAudioInputConstraints(listed, mixerState.headphones.selected_input_device_id);
			const stream = await withHeadphoneOperationTimeout(
				'getUserMedia',
				mediaDevices.getUserMedia({ audio, video: false })
			);
			// One source node for the whole run, kept pulled through a hard zero.
			// Measured Wed 16 Sep 2026 on the real modal: a fresh source per
			// capture, idle in between, made the speaker lag climb 81, 147, 273 ms
			// inside one run at a steady 0.52 peak, resetting only with a new stream.
			const source = ctx.createMediaStreamSource(stream);
			const keepalive = ctx.createGain();
			keepalive.gain.value = 0;
			source.connect(keepalive);
			keepalive.connect(ctx.destination);
			return {
				stream,
				source,
				stop: () => {
					source.disconnect();
					keepalive.disconnect();
					for (const track of stream.getTracks()) track.stop();
				}
			};
		},
		playTrain(bus, reference, sampleRate, signal, gain, whenSec, purpose = 'measure') {
			const target = _cueAlignChirpTarget(bus, purpose, nodes, ctx);
			return _playChirpTrain(ctx, target, reference, sampleRate, signal, gain, whenSec);
		},
		openCapture(mic, durationMs, _sampleRate, signal) {
			const source = (mic as _MicStreamHandle).source;
			if (!(source instanceof MediaStreamAudioSourceNode)) {
				throw new TypeError('cue alignment capture needs the MicHandle returned by getUserMedia');
			}
			return _openChirpCapture(ctx, source, durationMs, signal);
		},
		contextTimeSec: () => ctx.currentTime,
		sleep: _sleepMs,
		now: () => Date.now(),
		persist(result: AppliedAlignment) {
			setHeadDelayMs(result.head_delay_ms);
			setMasterDelayMs(result.master_delay_ms);
			persistMixerConfig({ last_calibration: result.record });
		},
		readDelays() {
			return {
				head_delay_ms: mixerState.headphones.head_delay_ms,
				master_delay_ms: mixerState.headphones.master_delay_ms
			};
		},
		setDelays(delays) {
			applyUnsavedAlignmentDelays(delays);
		},
		alignmentMode: () => mixerState.headphones.alignment_mode,
		deviceIds: () => ({ cue: cueId, master: mixerState.headphones.selected_master_output_device_id })
	};
}
