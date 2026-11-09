/** Types for the spec-faithful AudioParam stand-in (spec-audio-param.mjs). */
export declare class SpecAudioParam {
	constructor(defaultValue: number, sampleRate: number);
	value: number;
	readonly sampleRate: number;
	readonly calls: unknown[][];
	readonly events: unknown[];
	readonly rendered: number[];
	cancelScheduledValues(t: number): void;
	setTargetAtTime(v: number, t: number, tau: number): void;
	setValueCurveAtTime(curve: ArrayLike<number>, t: number, duration: number): void;
	valueAt(t: number): number;
	renderTo(frame: number): void;
}
