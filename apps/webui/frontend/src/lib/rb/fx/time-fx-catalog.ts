/**
 * Typed table of time-based beat FX effects (issue #9973, #10015). The unit,
 * panel TIME readout and CLI catalog read one frozen source of truth.
 * `builtOnMain` is true only where a kernel exists on main (issue #13726):
 * echo-kernel.ts hosts delay, echo and pingpong; the rest await their kernels.
 */

export type TimeFxLevelLaw = 'balance' | 'hold' | 'depth';
export type TimeFxRepeats = 'one' | 'many';

export type TimeFxEffectName =
	'delay' | 'echo' | 'pingpong' | 'spiral' | 'helix' | 'reverse_delay' | 'multitap' | 'tape';

export interface TimeFxCatalogRow {
	readonly name: TimeFxEffectName;
	readonly kernel: string;
	readonly mode: string;
	readonly builtOnMain: boolean;
	readonly beatsMin: number;
	readonly beatsMax: number;
	readonly timeMsMin: number;
	readonly timeMsMax: number;
	readonly levelLaw: TimeFxLevelLaw;
	readonly repeats: TimeFxRepeats;
	readonly defaultBeats: number;
	readonly source: string;
}

export const TIME_FX_CATALOG = Object.freeze([
	Object.freeze({
		name: 'delay',
		kernel: 'echo',
		mode: 'delay',
		builtOnMain: true,
		beatsMin: 1 / 16,
		beatsMax: 16,
		timeMsMin: 1,
		timeMsMax: 4000,
		levelLaw: 'balance',
		repeats: 'one',
		defaultBeats: 1,
		source: 'DJM-900NXS2 BEAT FX DELAY p.17'
	}),
	Object.freeze({
		name: 'echo',
		kernel: 'echo',
		mode: 'echo',
		builtOnMain: true,
		beatsMin: 1 / 16,
		beatsMax: 16,
		timeMsMin: 1,
		timeMsMax: 4000,
		levelLaw: 'balance',
		repeats: 'many',
		defaultBeats: 1,
		source: 'DJM-900NXS2 BEAT FX ECHO p.17'
	}),
	Object.freeze({
		name: 'pingpong',
		kernel: 'echo',
		mode: 'pingpong',
		builtOnMain: true,
		beatsMin: 1 / 16,
		beatsMax: 16,
		timeMsMin: 1,
		timeMsMax: 4000,
		levelLaw: 'balance',
		repeats: 'many',
		defaultBeats: 1,
		source: 'DJM-900NXS2 BEAT FX PING PONG p.17'
	}),
	Object.freeze({
		name: 'spiral',
		kernel: 'spiral',
		mode: 'spiral',
		builtOnMain: false,
		beatsMin: 1 / 16,
		beatsMax: 16,
		timeMsMin: 10,
		timeMsMax: 4000,
		levelLaw: 'depth',
		repeats: 'many',
		defaultBeats: 1,
		source: 'DJM-900NXS2 BEAT FX SPIRAL p.17'
	}),
	Object.freeze({
		name: 'helix',
		kernel: 'echo',
		mode: 'helix',
		builtOnMain: false,
		beatsMin: 1 / 16,
		beatsMax: 16,
		timeMsMin: 1,
		timeMsMax: 4000,
		levelLaw: 'hold',
		repeats: 'many',
		defaultBeats: 1,
		source: 'DJM-900NXS2 BEAT FX HELIX p.17'
	}),
	Object.freeze({
		name: 'reverse_delay',
		kernel: 'reverse_delay',
		mode: 'reverse',
		builtOnMain: false,
		beatsMin: 1 / 8,
		beatsMax: 16,
		timeMsMin: 1,
		timeMsMax: 4000,
		levelLaw: 'balance',
		repeats: 'one',
		defaultBeats: 1,
		source: 'DJM-800 REVERSE DELAY p.14'
	}),
	Object.freeze({
		name: 'multitap',
		kernel: 'multitap',
		mode: 'multitap',
		builtOnMain: false,
		beatsMin: 1 / 16,
		beatsMax: 16,
		timeMsMin: 10,
		timeMsMax: 4000,
		levelLaw: 'balance',
		repeats: 'many',
		defaultBeats: 1,
		source: 'DJM-2000 MULTI TAP DELAY p.22'
	}),
	Object.freeze({
		name: 'tape',
		kernel: 'echo',
		mode: 'tape',
		builtOnMain: false,
		beatsMin: 1 / 16,
		beatsMax: 16,
		timeMsMin: 1,
		timeMsMax: 4000,
		levelLaw: 'balance',
		repeats: 'many',
		defaultBeats: 1,
		source: 'Traktor Tape Delay; echo FXECHO-06'
	})
]) as readonly TimeFxCatalogRow[];

const _BY_NAME = new Map(TIME_FX_CATALOG.map((row) => [row.name, row]));

export const TIME_FX_EFFECT_NAMES = TIME_FX_CATALOG.map(
	(row) => row.name
) as readonly TimeFxEffectName[];

export const TIME_FX_BUILT_EFFECT_NAMES = TIME_FX_CATALOG.filter((row) => row.builtOnMain).map(
	(row) => row.name
) as readonly TimeFxEffectName[];

/** Catalog row for `name`, built or not, or null when the name is unknown. */
export function timeFxCatalogRow(name: string): TimeFxCatalogRow | null {
	return _BY_NAME.get(name as TimeFxEffectName) ?? null;
}

export function beatDivisionToMs(beats: number, bpm: number): number {
	if (!Number.isFinite(beats) || beats <= 0 || !Number.isFinite(bpm) || bpm <= 0) {
		throw new RangeError(`beat_fx division ${beats} at bpm ${bpm} is not finite and positive`);
	}
	return (beats * 60_000) / bpm;
}

/** Refuse a division or derived time outside the catalog row (issue #10015). */
export function assertTimeFxDivisionAllowed(effect: string, beats: number, bpm: number): void {
	const row = timeFxCatalogRow(effect);
	if (row === null) {
		throw new TypeError(`beat_fx effect ${effect} is not in the time catalog`);
	}
	if (beats < row.beatsMin || beats > row.beatsMax) {
		throw new RangeError(
			`beat_fx ${effect} beats ${beats} outside ${row.beatsMin}..${row.beatsMax} beats`
		);
	}
	const ms = beatDivisionToMs(beats, bpm);
	if (ms < row.timeMsMin || ms > row.timeMsMax) {
		throw new RangeError(
			`beat_fx ${effect} time ${ms.toFixed(3)} ms outside ${row.timeMsMin}..${row.timeMsMax} ms`
		);
	}
}
