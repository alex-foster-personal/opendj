/**
 * By-ear level calibration setters (#1475): R/M capture + toggle, split out
 * of prefs.svelte.ts the same way deck-layout-prefs.ts already splits the
 * MORE/LESS setters - a cohesive small concern with its own factory rather
 * than growing the reactive singleton module further.
 *
 * Deliberately NOT a rune module (no $state here): a pure factory the
 * reactive singleton in prefs.svelte.ts calls into with its own uiPrefs
 * state + persist/disk-sync hooks, mirroring makeDeckLayoutSetters exactly.
 */
import { CAL_CEILING_MAX_DBFS, CAL_MAX_DBFS, CAL_MIN_DBFS } from './prefs-fields';
import type { LevelCalibrationPrefs } from './prefs-types';

/** Which half of the calibration a control captures/toggles. */
export type LevelCalibrationKind = 'red' | 'ceiling';

/** The slice of RbUiPrefs the setters below read and write. */
export interface LevelCalibrationPrefsState {
	level_calibration: LevelCalibrationPrefs;
}

export interface LevelCalibrationSetters {
	setLevelCalibrationCapture(kind: LevelCalibrationKind, dbfs: number): void;
	setLevelCalibrationDisabled(kind: LevelCalibrationKind): void;
}

/**
 * Build the R/M calibration setters against a live prefs state + the
 * caller's persist/disk-sync hooks. `state` is passed by reference (the
 * caller's reactive $state object) and mutated in place here - a Svelte 5
 * $state proxy stays reactive regardless of which module holds the
 * reference that mutates it.
 */
export function makeLevelCalibrationSetters(
	state: LevelCalibrationPrefsState,
	persist: () => void,
	// Returns the in-flight write so the chain below can actually serialize.
	// Typed `=> void` until Tue 9 Sep 2026, which silently defeated the chain:
	// `.then()` on undefined resolves immediately and every write raced.
	syncDiskPrefs: (patch: { level_calibration: LevelCalibrationPrefs }) => Promise<void>
): LevelCalibrationSetters {
	/** Off -> click (re)captures the level at the tap right now and arms it.
	 * Always (re)captures: there is no separate "re-enable the old number"
	 * gesture, so recalibrating to a new loud track is just another click. */
	/**
	 * Disk writes are chained, never fired in parallel.
	 *
	 * Each R/M action PUTs a full snapshot of BOTH halves. Two clicks in quick
	 * succession raced: the requests could land out of order and an older
	 * snapshot could overwrite a newer one, so the live tab looked right and
	 * the lost toggle reappeared after a reload. Chaining makes the last call
	 * the last write. A rejected PUT does not break the chain: the local state
	 * stays authoritative (same contract as the fire-and-forget it replaces),
	 * and the NEXT write still goes out.
	 */
	let writeChain: Promise<void> = Promise.resolve();

	function queueDiskWrite(): void {
		const snapshot = { ...state.level_calibration };
		writeChain = writeChain.then(
			() => syncDiskPrefs({ level_calibration: snapshot }),
			() => syncDiskPrefs({ level_calibration: snapshot })
		);
	}

	function setLevelCalibrationCapture(kind: LevelCalibrationKind, dbfs: number): void {
		// Reject rather than clamp: persisting a value outside prefs-fields.ts's
		// own bounds would round-trip fine now and throw on the NEXT load,
		// bricking the uiPrefs singleton (it initializes at module scope with
		// no try/catch) until localStorage is cleared by hand. ceiling gets the
		// tighter 0 dBFS upper bound: min(1, 10**(dbfs/20)) is a no-op for any
		// dbfs above 0, so a capture above it would arm M while leaving the
		// master gain untouched.
		const max = kind === 'ceiling' ? CAL_CEILING_MAX_DBFS : CAL_MAX_DBFS;
		if (!Number.isFinite(dbfs) || dbfs < CAL_MIN_DBFS || dbfs > max) {
			throw new RangeError(
				`setLevelCalibrationCapture: ${kind} dbfs must be finite and between ${CAL_MIN_DBFS} ` +
					`and ${max}, got ${dbfs}`
			);
		}
		if (kind === 'red') {
			state.level_calibration.red_dbfs = dbfs;
			state.level_calibration.red_enabled = true;
		} else {
			state.level_calibration.ceiling_dbfs = dbfs;
			state.level_calibration.ceiling_enabled = true;
		}
		persist();
		queueDiskWrite();
	}

	/** On -> click disarms, keeping the captured number so a later re-arm
	 * does not need a fresh capture. */
	function setLevelCalibrationDisabled(kind: LevelCalibrationKind): void {
		if (kind === 'red') state.level_calibration.red_enabled = false;
		else state.level_calibration.ceiling_enabled = false;
		persist();
		queueDiskWrite();
	}

	return { setLevelCalibrationCapture, setLevelCalibrationDisabled };
}
