/**
 * PPM level-meter policy. Pure, and the ONLY place a metering number is decided.
 *
 * Split the same way as `xrun-math` / `xrun-sentinel-processor`: the worklet
 * measures (peak and RMS of a render quantum) and decides nothing; every
 * threshold, ballistic rate and color band lives here, on the main thread,
 * where it can be unit tested without an AudioContext.
 *
 * The behaviour modelled is a Peak Programme Meter (IEC 60268-10), not a VU
 * meter: instantaneous rise so a kick transient lights the bar, and a slow
 * fixed-rate fall so the eye can read it. That asymmetry is most of what makes
 * a meter feel like DJ hardware rather than a raw level readout.
 *
 * Background and the standards it comes from:
 * docs/research/adrian-level-meters-clipping-lights.md
 */

// --------------------------------------------------------------------------
// config
// --------------------------------------------------------------------------

/** Bottom of the displayed scale. Below this the meter reads empty. */
export const METER_FLOOR_DBFS = -60;

/**
 * EBU PPM fallback: 20 dB in 1.7 s. Expressed as a rate so a dropped frame
 * decays by the elapsed time rather than by one fixed step, which is what
 * keeps the fall speed identical at 30 and 144 Hz.
 */
const PPM_DECAY_DB_PER_S = 20 / 1.7;

/** How long the peak marker parks at its maximum before it starts falling. */
const PEAK_HOLD_S = 1.0;

/**
 * Ten segment thresholds, in dBFS, spaced logarithmically. A segment lights
 * when the level is at or above its threshold. Linear-amplitude spacing would
 * leave the bottom eight dark for all real music, which is the single most
 * common way a software meter ends up useless.
 */
export const SEGMENT_THRESHOLDS_DBFS: readonly number[] = Object.freeze([
	-34, -26, -20, -16, -12, -9, -6, -3, -1.5, 0
]);

/** Segments 1-4 green, 5-7 amber, 8-10 red. Indices are 1-based segment counts. */
const AMBER_FROM_SEGMENT = 5;
const RED_FROM_SEGMENT = 8;

/**
 * Latched-clip threshold. Not 0.0: a sample peak of exactly full scale is
 * already suspicious, and true peak between samples is higher than anything
 * this meter can see, so the warning is raised just below.
 */
export const CLIP_DBFS = -0.1;

/** Amplitude floor before the log, so digital silence is finite, not -Infinity. */
const AMPLITUDE_EPSILON = 1e-7;

export type MeterBand = 'green' | 'amber' | 'red';

// --------------------------------------------------------------------------
// scale
// --------------------------------------------------------------------------

/** dBFS for a linear amplitude in [0, 1]. Silence floors, it does not diverge. */
export function dbfsFromAmplitude(amplitude: number): number {
	if (!Number.isFinite(amplitude)) {
		throw new RangeError(`dbfsFromAmplitude: amplitude must be finite, got ${amplitude}`);
	}
	return 20 * Math.log10(Math.max(Math.abs(amplitude), AMPLITUDE_EPSILON));
}

/** Position on the displayed scale, 0 at the floor and 1 at full scale. */
export function normalizedFromDbfs(db: number): number {
	if (!Number.isFinite(db)) {
		throw new RangeError(`normalizedFromDbfs: db must be finite, got ${db}`);
	}
	const clamped = Math.min(0, Math.max(METER_FLOOR_DBFS, db));
	return (clamped - METER_FLOOR_DBFS) / -METER_FLOOR_DBFS;
}

// --------------------------------------------------------------------------
// ballistics
// --------------------------------------------------------------------------

/**
 * One PPM step: rise instantly to the observed level, otherwise fall toward it
 * at the fixed rate. Never falls past the observed level, so a steady tone
 * settles rather than oscillating.
 */
export function stepBallistics(previousDb: number, observedDb: number, elapsedS: number): number {
	if (!Number.isFinite(elapsedS) || elapsedS < 0) {
		throw new RangeError(`stepBallistics: elapsedS must be finite and >= 0, got ${elapsedS}`);
	}
	if (observedDb >= previousDb) return observedDb;
	return Math.max(observedDb, previousDb - PPM_DECAY_DB_PER_S * elapsedS);
}

export interface PeakHoldState {
	/** Held peak level, dBFS. */
	readonly db: number;
	/** Seconds the current peak has been held. Resets to 0 on a new maximum. */
	readonly heldS: number;
}

export const INITIAL_PEAK_HOLD: PeakHoldState = Object.freeze({
	db: METER_FLOOR_DBFS,
	heldS: 0
});

/**
 * Peak marker: jump to a new maximum and restart the hold, else sit still for
 * PEAK_HOLD_S and only then fall at the PPM rate.
 */
export function stepPeakHold(
	state: PeakHoldState,
	observedDb: number,
	elapsedS: number
): PeakHoldState {
	if (!Number.isFinite(elapsedS) || elapsedS < 0) {
		throw new RangeError(`stepPeakHold: elapsedS must be finite and >= 0, got ${elapsedS}`);
	}
	if (observedDb >= state.db) return { db: observedDb, heldS: 0 };
	const heldS = state.heldS + elapsedS;
	if (heldS < PEAK_HOLD_S) return { db: state.db, heldS };
	// Only the time past the hold window decays, so the fall rate does not
	// depend on where the frame boundary happened to land.
	const fallingS = Math.min(elapsedS, heldS - PEAK_HOLD_S);
	return {
		db: Math.max(observedDb, state.db - PPM_DECAY_DB_PER_S * fallingS),
		heldS
	};
}

// --------------------------------------------------------------------------
// segments
// --------------------------------------------------------------------------

/** How many of the ten segments are lit at this level. 0 means below the first. */
export function segmentsLitFromDbfs(db: number): number {
	if (!Number.isFinite(db)) {
		throw new RangeError(`segmentsLitFromDbfs: db must be finite, got ${db}`);
	}
	let lit = 0;
	for (const threshold of SEGMENT_THRESHOLDS_DBFS) {
		if (db >= threshold) lit += 1;
	}
	return lit;
}

/** Color band for a 1-based segment number. Throws outside the ten segments. */
export function segmentBand(segment: number): MeterBand {
	if (!Number.isInteger(segment) || segment < 1 || segment > SEGMENT_THRESHOLDS_DBFS.length) {
		throw new RangeError(
			`segmentBand: segment must be an integer in 1..${SEGMENT_THRESHOLDS_DBFS.length}, got ${segment}`
		);
	}
	if (segment >= RED_FROM_SEGMENT) return 'red';
	if (segment >= AMBER_FROM_SEGMENT) return 'amber';
	return 'green';
}

/** True once the level is close enough to full scale to call it clipping. */
export function isClipping(db: number): boolean {
	if (!Number.isFinite(db)) {
		throw new RangeError(`isClipping: db must be finite, got ${db}`);
	}
	return db >= CLIP_DBFS;
}
