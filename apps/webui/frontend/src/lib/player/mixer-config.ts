/**
 * Persisted mixer configuration that survives reloads.
 *
 * Master, crossfader, headphone mix, and level stay session-only. Only values
 * that must survive a restart live here: CUEOUT-03 head delay, and the
 * CUEOUT-14 alignment mode, room delay and last calibration result.
 *
 * The blob is a PATCH target, not a whole document: `persistMixerConfig`
 * merges the keys it is given over the keys already stored, so a blob written
 * before CUEOUT-14 (only `head_delay_ms`) stays loadable and a setter for one
 * field never forgets another.
 */

import {
	assertHeadDelayMs,
	assertHeadphoneAlignmentMode,
	assertMasterDelayMs,
	type HeadphoneAlignmentMode
} from '$lib/player/constants';

const MIXER_CONFIG_STORAGE_KEY = 'mdt.rb.mixer-config.v1';

/** The last successful cue/master measurement, so switching alignment mode
 * re-derives the delays without re-measuring. */
export interface CueCalibrationRecord {
	cue_latency_ms: number;
	master_latency_ms: number;
	/** ISO-8601 instant of the measurement. */
	measured_at: string;
	cue_device_id: string;
	master_device_id: string | null;
}

export interface MixerConfig {
	head_delay_ms: number;
	alignment_mode: HeadphoneAlignmentMode;
	master_delay_ms: number;
	last_calibration: CueCalibrationRecord | null;
}

const DEFAULT_MIXER_CONFIG: Readonly<MixerConfig> = Object.freeze({
	head_delay_ms: 0,
	alignment_mode: 'hybrid',
	master_delay_ms: 0,
	last_calibration: null
});

function _storage(): Storage | null {
	if (typeof window === 'undefined') return null;
	return window.localStorage ?? null;
}

function _assertFiniteMs(name: string, value: unknown): asserts value is number {
	if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
		throw new RangeError(`${name} must be a finite non-negative number, got ${String(value)}`);
	}
}

export function assertCueCalibrationRecord(record: unknown): asserts record is CueCalibrationRecord {
	if (record === null || typeof record !== 'object') {
		throw new TypeError(`last_calibration must be an object, got ${String(record)}`);
	}
	const candidate = record as Record<string, unknown>;
	_assertFiniteMs('last_calibration.cue_latency_ms', candidate.cue_latency_ms);
	_assertFiniteMs('last_calibration.master_latency_ms', candidate.master_latency_ms);
	if (typeof candidate.measured_at !== 'string' || Number.isNaN(Date.parse(candidate.measured_at))) {
		throw new TypeError(`last_calibration.measured_at must be an ISO-8601 string, got ${String(candidate.measured_at)}`);
	}
	if (typeof candidate.cue_device_id !== 'string' || candidate.cue_device_id.trim() === '') {
		throw new TypeError('last_calibration.cue_device_id must be a non-empty string');
	}
	if (candidate.master_device_id !== null && typeof candidate.master_device_id !== 'string') {
		throw new TypeError('last_calibration.master_device_id must be a string or null');
	}
}

/** Validate every key present in a partial config. Absent keys are fine;
 * present keys must be legal. */
function _assertMixerConfigPatch(patch: Partial<MixerConfig>): void {
	if (patch.head_delay_ms !== undefined) assertHeadDelayMs(patch.head_delay_ms);
	if (patch.alignment_mode !== undefined) assertHeadphoneAlignmentMode(patch.alignment_mode);
	if (patch.master_delay_ms !== undefined) assertMasterDelayMs(patch.master_delay_ms);
	if (patch.last_calibration !== undefined && patch.last_calibration !== null) {
		assertCueCalibrationRecord(patch.last_calibration);
	}
}

function _malformed(cause: string): Error {
	return new Error(
		`${MIXER_CONFIG_STORAGE_KEY}: malformed blob (${cause}) - clear the localStorage key to recover`
	);
}

/** The stored keys only, validated. Missing key yields an empty patch. */
function _loadStoredPatch(): Partial<MixerConfig> {
	const storage = _storage();
	if (storage === null) return {};
	const raw = storage.getItem(MIXER_CONFIG_STORAGE_KEY);
	if (raw === null) return {};
	const parsed = JSON.parse(raw) as Partial<MixerConfig> | null;
	if (parsed === null || typeof parsed !== 'object') {
		throw _malformed('must be an object');
	}
	try {
		_assertMixerConfigPatch(parsed);
	} catch (cause) {
		throw _malformed((cause as Error).message);
	}
	return parsed;
}

/** Load persisted mixer config. Missing keys yield defaults; malformed blobs throw. */
export function loadMixerConfig(): MixerConfig {
	const stored = _loadStoredPatch();
	return {
		head_delay_ms: stored.head_delay_ms ?? DEFAULT_MIXER_CONFIG.head_delay_ms,
		alignment_mode: stored.alignment_mode ?? DEFAULT_MIXER_CONFIG.alignment_mode,
		master_delay_ms: stored.master_delay_ms ?? DEFAULT_MIXER_CONFIG.master_delay_ms,
		last_calibration:
			stored.last_calibration === undefined ? DEFAULT_MIXER_CONFIG.last_calibration : stored.last_calibration
	};
}

/** Validate and persist a partial mixer config over whatever is stored. */
export function persistMixerConfig(patch: Partial<MixerConfig>): void {
	_assertMixerConfigPatch(patch);
	const storage = _storage();
	if (storage === null) return;
	storage.setItem(MIXER_CONFIG_STORAGE_KEY, JSON.stringify({ ..._loadStoredPatch(), ...patch }));
}
