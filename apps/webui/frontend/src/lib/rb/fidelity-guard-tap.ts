/**
 * AUDFID-03: the guard's Web Audio side. A stereo tap on the master bus AFTER
 * the practice / split-cable stage (so a mono fold is seen) and the neutral
 * preconditions, read through narrow accessors the engine passes in.
 */
import { FIDELITY_GUARD_CFG, type StereoWindow } from '$lib/player/fidelity-guard';
import type { FidelitySample } from '$lib/rb/fidelity-guard-runtime';
import { stemControlsNeutral } from '$lib/rb/stem-neutral-bypass';
import type { MixerChannelState } from '$lib/rb/mixer-types';
import type { StemDeckState } from '$lib/rb/stem-types';

export interface MasterStereoTap {
	read(): StereoWindow;
	disconnect(): void;
}

/** Two analysers behind a splitter, fed from `bus`. Pure observers. */
export function createMasterStereoTap(ctx: BaseAudioContext, bus: AudioNode): MasterStereoTap {
	const splitter = ctx.createChannelSplitter(2);
	const left = ctx.createAnalyser();
	const right = ctx.createAnalyser();
	left.fftSize = right.fftSize = FIDELITY_GUARD_CFG.WINDOW_FRAMES;
	bus.connect(splitter);
	splitter.connect(left, 0);
	splitter.connect(right, 1);
	const l = new Float32Array(FIDELITY_GUARD_CFG.WINDOW_FRAMES);
	const r = new Float32Array(FIDELITY_GUARD_CFG.WINDOW_FRAMES);
	return {
		read: () => {
			left.getFloatTimeDomainData(l);
			right.getFloatTimeDomainData(r);
			return { left: l.slice(), right: r.slice(), sampleRate: ctx.sampleRate };
		},
		disconnect: () => {
			bus.disconnect(splitter);
			splitter.disconnect();
		}
	};
}

/** The decoded source window ending at `positionSec`. A mono file is read as
 * L = R. Null when the window falls outside the file. */
export function sourceWindowAt(buffer: AudioBuffer, positionSec: number, frames: number): StereoWindow | null {
	const end = Math.round(positionSec * buffer.sampleRate);
	const start = end - frames;
	if (start < 0 || end > buffer.length) return null;
	const left = buffer.getChannelData(0).slice(start, end);
	const right = buffer.numberOfChannels > 1 ? buffer.getChannelData(1).slice(start, end) : left.slice();
	return { left, right, sampleRate: buffer.sampleRate };
}

export interface GuardDeckView {
	playing: boolean;
	audible: boolean;
	stems: Pick<StemDeckState, 'status' | 'controls'>;
	channel: MixerChannelState;
	tempoRatio: number;
	keyShiftSemitones: number;
	buffer: AudioBuffer | null;
	positionSec: number;
}

export const FILTER_NEUTRAL = 0.5;
const TEMPO_EPS = 1e-3;

/** AUDFID-03: the neutral-state gate, then the two windows. Every failed
 * precondition is a named skip, never a pass. */
export function sampleFidelity(args: {
	decks: readonly GuardDeckView[];
	tap: MasterStereoTap | null;
	contextSampleRate: number;
	operatorSplit: boolean;
	cueBlendOnMain: boolean;
}): FidelitySample {
	if (args.tap === null) return { skip: 'no master bus tap (audio graph not built)' };
	if (args.operatorSplit) return { skip: 'split cue chosen by the operator: MAIN is mono by design' };
	if (args.cueBlendOnMain) return { skip: 'practice cue blend is on MAIN' };
	const audible = args.decks.filter((deck) => deck.playing && deck.audible);
	if (audible.length !== 1) return { skip: `${audible.length} decks audible on MAIN (needs exactly one)` };
	const deck = audible[0];
	if (!stemControlsNeutral(deck.stems, deck.channel)) return { skip: 'stem controls or EQ not neutral' };
	if (deck.channel.filter !== FILTER_NEUTRAL) return { skip: 'filter not neutral' };
	if (Math.abs(deck.tempoRatio - 1) > TEMPO_EPS || deck.keyShiftSemitones !== 0) return { skip: 'tempo or key shifted' };
	if (deck.buffer === null) return { skip: 'no decoded source' };
	if (deck.buffer.sampleRate !== args.contextSampleRate) return { skip: 'source and context sample rates differ' };
	const source = sourceWindowAt(deck.buffer, deck.positionSec, FIDELITY_GUARD_CFG.WINDOW_FRAMES);
	if (source === null) return { skip: 'window outside the track' };
	return { master: args.tap.read(), source };
}
