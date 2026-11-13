/**
 * Beat FX REVERB (issue #9821) on the engine on main: the SPACE convolver and its seeded impulse
 * (`spaceImpulseBuffer`). Amount sets the impulse RT60, TIME the room size (pre-delay scales with
 * it), X-PAD a lowpass on the wet, LEVEL/DEPTH the wet. The FDN kernel (#9817) replaces the voice
 * behind this same surface once it lands.
 *
 * ```
 * send -> input -> [gate -> pre-delay -> convolver -> fade] per voice -> lowpass -> wet -> return
 * ```
 *
 * The slot is a send and return (the channel dry stays 1), so wet 0 returns exactly 0. Off closes
 * the unit's send and the tail rings out. A new amount or TIME starts a fresh voice: the old gate
 * closes over 10 ms and its tail rings on; the voice before it fades out over 10 ms. `cut`
 * (Release FX) fades every voice over 10 ms and starts a clean one.
 */
import {
	DEFAULT_REVERB_SETTINGS,
	REVERB_PREDELAY_SEC,
	REVERB_RAMP_SEC,
	REVERB_SIZE_RANGE,
	assertReverbRange,
	reverbAmountToRt60,
	reverbTimeToSize,
	reverbWetForLevel,
	type BeatFxReverbSettings
} from '../../beat-fx-reverb';
import { spaceImpulseBuffer } from '../../color-fx-graph';
import { RecordingKernelHost, type BeatFxKernel } from './kernel-host';
import { registerBeatFxEffect } from './registry';

/** Butterworth corner: Web Audio lowpass Q is in dB, -3.01 dB is 0.707 linear. */
const LOWPASS_Q_DB = -3.0103;

interface _Voice {
	readonly key: string;
	readonly gate: GainNode;
	readonly nodes: readonly AudioNode[];
	readonly fade: GainNode;
}

function _ramp(param: AudioParam, from: number, to: number, at: number): void {
	param.cancelScheduledValues(at);
	param.setValueAtTime(from, at);
	param.linearRampToValueAtTime(to, at + REVERB_RAMP_SEC);
}

/** Build the reverb for the unit's slot, voiced at the default amount, TIME and filter. */
export function createReverbSlotKernel(ctx: BaseAudioContext): BeatFxKernel {
	const input = ctx.createGain();
	const lowpass = ctx.createBiquadFilter();
	const wet = ctx.createGain();
	const output = ctx.createGain();
	lowpass.type = 'lowpass';
	lowpass.Q.value = LOWPASS_Q_DB;
	lowpass.frequency.value = DEFAULT_REVERB_SETTINGS.filter_hz;
	wet.gain.value = 0;
	lowpass.connect(wet).connect(output);
	let settings: BeatFxReverbSettings = { ...DEFAULT_REVERB_SETTINGS };
	let depth = 0;
	let live: _Voice | null = null;
	let ringing: _Voice | null = null;
	let fading: _Voice[] = [];

	const voiceKey = (s: BeatFxReverbSettings): string => `${s.amount}:${s.time}`;
	const startVoice = (at: number): void => {
		const [gate, preDelay, convolver, fade] = [
			ctx.createGain(),
			ctx.createDelay(REVERB_PREDELAY_SEC * REVERB_SIZE_RANGE[1]),
			ctx.createConvolver(),
			ctx.createGain()
		];
		preDelay.delayTime.value = REVERB_PREDELAY_SEC * reverbTimeToSize(settings.time);
		convolver.normalize = true;
		convolver.buffer = spaceImpulseBuffer(ctx, reverbAmountToRt60(settings.amount));
		input.connect(gate).connect(preDelay).connect(convolver).connect(fade).connect(lowpass);
		_ramp(gate.gain, 0, 1, at);
		live = { key: voiceKey(settings), gate, fade, nodes: [gate, preDelay, convolver, fade] };
	};
	/** Close the live voice (it rings, or with `cutLive` fades too) and fade the one before it. */
	const retire = (at: number, cutLive: boolean): void => {
		for (const old of fading) for (const node of old.nodes) node.disconnect();
		fading = ringing === null ? [] : [ringing];
		ringing = null;
		if (live !== null) {
			_ramp(live.gate.gain, 1, 0, at);
			if (cutLive) fading.push(live);
			else ringing = live;
		}
		for (const old of fading) _ramp(old.fade.gain, 1, 0, at);
		live = null;
	};
	startVoice(ctx.currentTime);

	return {
		input,
		output,
		setTiming: () => undefined,
		setDepth: (next, at) => {
			const from = reverbWetForLevel(depth);
			depth = next;
			_ramp(wet.gain, from, reverbWetForLevel(next), at);
		},
		setReverb: (next, at) => {
			const filterHz = assertReverbRange('filter_hz', next.filter_hz);
			reverbAmountToRt60(next.amount);
			reverbTimeToSize(next.time);
			_ramp(lowpass.frequency, settings.filter_hz, filterHz, at);
			const retune = voiceKey(next) !== voiceKey(settings);
			settings = { ...next };
			if (retune) {
				retire(at, false);
				startVoice(at);
			}
		},
		cut: (at) => {
			retire(at, true);
			startVoice(at + REVERB_RAMP_SEC);
		},
		dispose: () => {
			for (const old of [live, ringing, ...fading])
				for (const node of old?.nodes ?? []) node.disconnect();
			for (const node of [input, lowpass, wet, output]) node.disconnect();
		}
	};
}

registerBeatFxEffect({
	name: 'reverb',
	levelLaw: 'depth',
	builtOnMain: true,
	usesDivision: false,
	assertDivisionAllowed: () => undefined,
	createHost: () => new RecordingKernelHost('reverb'),
	createKernel: createReverbSlotKernel
});
