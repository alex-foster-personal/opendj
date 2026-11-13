/**
 * Free TIME in milliseconds beside the beat division (issue #10001), pure.
 * Spec: `(private path)` section 3 and
 * `01-beat-fx.md` section 4.3. Mimicked: TIME 1 to 4000 ms for DELAY, ECHO and
 * PING PONG (DJM-900NXS Operating Instructions p. 17; DJM-A9 TIME encoder p. 52).
 * The bounds come from the time catalog rows; the log pad mapping is ours (hygiene).
 */
import { BEAT_FX_BEATS, type BeatFxBeats } from '../beat-fx';
import { beatDivisionToMs, type TimeFxCatalogRow } from './time-fx-catalog';

/** Ends of the X-PAD's free-time scale in ms. */
export const XPAD_MS_MIN = 1;
export const XPAD_MS_MAX = 4000;

const _DIVISION_LABELS = new Map<number, string>([
	[1 / 16, '1/16'],
	[1 / 8, '1/8'],
	[1 / 4, '1/4'],
	[1 / 2, '1/2'],
	[3 / 4, '3/4'],
	[1, '1'],
	[2, '2'],
	[4, '4'],
	[8, '8'],
	[16, '16']
]);

/** The nearest beat division to a free time, and how far off it is. */
export interface NearestBeats {
	beats: BeatFxBeats;
	/** (ms - division ms) as a percent of one beat at the tempo. */
	errorPercentOfBeat: number;
}

/** A beat division at `bpm` in seconds; throws on a non-finite or non-positive input. */
export function beatsToSeconds(beats: number, bpm: number): number {
	return beatDivisionToMs(beats, bpm) / 1000;
}

/** The division list entry nearest `ms` at `bpm`, with the error in percent of a beat. */
export function msToNearestBeats(ms: number, bpm: number): NearestBeats {
	if (!Number.isFinite(ms) || ms <= 0) {
		throw new RangeError(`beat_fx time_ms ${ms} is not finite and positive`);
	}
	const beatMs = beatDivisionToMs(1, bpm);
	let best: BeatFxBeats = BEAT_FX_BEATS[0];
	for (const beats of BEAT_FX_BEATS) {
		if (Math.abs(ms - beats * beatMs) < Math.abs(ms - best * beatMs)) best = beats;
	}
	return { beats: best, errorPercentOfBeat: ((ms - best * beatMs) / beatMs) * 100 };
}

/** The panel label of a division, `1/16` to `16` and `3/4`; throws off the list. */
export function divisionLabel(beats: number): string {
	const label = _DIVISION_LABELS.get(beats);
	if (label === undefined) {
		throw new RangeError(`beat_fx beats ${beats} is not one of ${BEAT_FX_BEATS.join('|')}`);
	}
	return label;
}

/** X-PAD position 0..1 to a free time on a log scale from 1 to 4000 ms. */
export function xpadToMs(x: number): number {
	if (typeof x !== 'number' || !Number.isFinite(x) || x < 0 || x > 1) {
		throw new RangeError(`beat_fx xpad must be a number in 0..1; got ${String(x)}`);
	}
	return XPAD_MS_MIN * (XPAD_MS_MAX / XPAD_MS_MIN) ** x;
}

/** Refuse a free time that is not finite or outside the catalog row, naming the value and the bound. */
export function assertTimeMs(ms: number, row: TimeFxCatalogRow): number {
	if (typeof ms !== 'number' || !Number.isFinite(ms)) {
		throw new RangeError(`beat_fx ${row.name} time_ms must be a finite number; got ${String(ms)}`);
	}
	if (ms < row.timeMsMin || ms > row.timeMsMax) {
		throw new RangeError(
			`beat_fx ${row.name} time_ms ${ms} outside ${row.timeMsMin}..${row.timeMsMax} ms`
		);
	}
	return ms;
}
