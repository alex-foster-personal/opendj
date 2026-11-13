/**
 * Beat FX kernel host contract and the state-only recording host (issues #9782, #13726).
 * Lives beside the registry so effect files import it without a cycle through the unit.
 */
import type { BeatFxReverbSettings } from '../../beat-fx-reverb';

/** Which level setter a level law drives. */
export type BeatFxLevelLaw = 'balance' | 'hold' | 'depth';

export interface BeatFxKernelHost {
	readonly effect: string;
	setWetDryBalance(level: number, at: number): void;
	setHold?(hold: number, at: number): void;
	setDepth?(depth: number, at: number): void;
	setTiming?(seconds: number, at: number): void;
	dispose(): void;
}

/** One kernel in the unit's slot (spec 01 section 4.2); families 04 to 08 implement it. */
export interface BeatFxKernel {
	/** Where the unit's sends land. */
	readonly input: AudioNode;
	/** The wet return; the unit joins it to masterGain. */
	readonly output: AudioNode;
	/** Move the kernel to `seconds` at context time `at` through its own retune, never a delay-time slide. */
	setTiming(seconds: number, at: number): void;
	/** Wet depth 0..1 from context time `at`; 0 is silent. */
	setDepth(depth: number, at: number): void;
	/** REVERB only (issue #9821): amount, TIME and filter from context time `at`. */
	setReverb?(settings: BeatFxReverbSettings, at: number): void;
	/** Release FX cut: ramp the wet to 0 over 10 ms and clear the kernel's state. */
	cut?(at: number): void;
	dispose(): void;
}

/** Records the last level setter each effect reached; no audio until #9782 wires the unit graph. */
export class RecordingKernelHost implements BeatFxKernelHost {
	readonly effect: string;
	lastLevelKind: BeatFxLevelLaw | null = null;
	lastLevel = NaN;
	wetDry = 1;
	hold = 0;
	depth = 0;
	disposed = false;

	constructor(effect: string) {
		this.effect = effect;
	}

	setWetDryBalance(level: number, _at: number): void {
		this.lastLevelKind = 'balance';
		this.lastLevel = level;
		this.wetDry = level;
	}

	setHold(hold: number, _at: number): void {
		this.lastLevelKind = 'hold';
		this.lastLevel = hold;
		this.hold = hold;
	}

	setDepth(depth: number, _at: number): void {
		this.lastLevelKind = 'depth';
		this.lastLevel = depth;
		this.depth = depth;
	}

	dispose(): void {
		this.disposed = true;
	}
}
