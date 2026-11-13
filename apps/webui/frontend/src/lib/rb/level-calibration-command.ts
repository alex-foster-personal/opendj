/**
 * Shared R|M level-calibration apply path (#1475, #5586).
 *
 * The ChannelStrip R/M click and the `level_calibration` performance command
 * both go through `applyLevelCalibration`, so an agent (or a LESS user, who
 * cannot see the buttons) arms and disarms exactly as a MORE click does, and
 * the stopped-deck refusal cannot drift between the two.
 */
import { getDeckState, peekDeckMeterReading } from '$lib/rb/audio-engine.svelte';
import type { DeckId } from '$lib/rb/deck-slots';
import type { LevelCalibrationKind } from '$lib/rb/level-calibration-prefs';
import { METER_FLOOR_DBFS, calibrationCaptureDb } from '$lib/rb/meter-math';
import { setLevelCalibrationCapture, setLevelCalibrationDisabled } from '$lib/rb/prefs.svelte';

/** The sentence the R/M button title gives for why a capture cannot happen. */
export const LEVEL_CALIBRATION_REFUSAL =
	'A stopped or silent channel captures nothing: there is no level to calibrate against.';

export type LevelCalibrationResult = { ok: true } | { ok: false; reason: string };

type MeterReading = { readonly db: number; readonly heardDb: number };

/** Live inputs the capture reads; swappable so tests need no AudioGraph. */
interface LevelCalibrationSources {
	playingOf: (deck: DeckId) => boolean;
	readingOf: (deck: DeckId) => MeterReading;
}

const LIVE_SOURCES: LevelCalibrationSources = {
	playingOf: (deck) => getDeckState(deck).playing,
	readingOf: (deck) => peekDeckMeterReading(deck)
};

let _sources: LevelCalibrationSources = LIVE_SOURCES;

/** Test injector: fake the transport and meter reading; null restores the live ones. */
export function setLevelCalibrationSourcesForTest(sources: LevelCalibrationSources | null): void {
	_sources = sources ?? LIVE_SOURCES;
}

/** The R/M button label a refusal names. */
export function levelCalibrationLabel(kind: LevelCalibrationKind): string {
	return kind === 'red' ? 'Meter red anchor' : 'Master output ceiling';
}

/**
 * Arm (capture the deck's current level) or disarm one calibration kind.
 * Disarm always succeeds and keeps the captured number. Arm is refused while
 * the deck is not playing, or while its tap sits at the meter floor.
 */
export function applyLevelCalibration(
	deck: DeckId,
	kind: LevelCalibrationKind,
	enabled: boolean
): LevelCalibrationResult {
	if (!enabled) {
		setLevelCalibrationDisabled(kind);
		return { ok: true };
	}
	const refused: LevelCalibrationResult = {
		ok: false,
		reason: `${levelCalibrationLabel(kind)}: deck ${deck} not captured. ${LEVEL_CALIBRATION_REFUSAL}`
	};
	// A stopped deck is refused on TRANSPORT STATE, not just on level. The
	// numeric floor alone was not enough: the meter's PPM ballistics decay
	// at ~11.8 dB/s, so for several seconds after a pause the tap still
	// reads a real-looking value on its way down. Capturing mid-decay arms a
	// ceiling from a number that describes nothing, and a quiet tail arms a
	// severe master attenuation. `playing` is the only signal that says the
	// level means something right now.
	if (!_sources.playingOf(deck)) return refused;
	// R and M capture the post-channel-fader level the bar draws (`db`, pin
	// 3dc62dbb909c); the clip pip alone uses post-crossfader heard peaks.
	const db = calibrationCaptureDb(kind, _sources.readingOf(deck));
	// Belt and braces: a playing deck can still sit at the floor (silence in
	// the track, or a graph not yet producing), and a -60 dBFS ceiling is a
	// 0.001 master multiplier, i.e. one click silences the output.
	if (db <= METER_FLOOR_DBFS) return refused;
	setLevelCalibrationCapture(kind, db);
	return { ok: true };
}
