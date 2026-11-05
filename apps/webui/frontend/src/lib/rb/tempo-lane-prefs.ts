/**
 * Tempo-change lane prefs (PR #8342), persisted beside STRUCT-05's
 * show_sections and plumbed the same way (section-overlay-prefs.ts).
 *
 * - show_tempo_lane: paint the deck strip's tempo lane and the main
 *   waveform's tempo curve. Default ON: the lane only paints where the
 *   beatgrid actually moves, so a steady track shows nothing new.
 * - tempo_lane_scale: the curve's vertical scale. 'fixed_10' = full height
 *   is 10 percent off nominal BPM; 'relative' = full height is the track's
 *   own largest change, floored at 2 percent.
 *
 * Not a rune module - the reactive singleton passes its $state object in.
 */
export const TEMPO_LANE_SCALES = ['fixed_10', 'relative'] as const;
export type TempoLaneScale = (typeof TEMPO_LANE_SCALES)[number];

export interface TempoLanePrefsState {
	show_tempo_lane: boolean;
	tempo_lane_scale: TempoLaneScale;
}

export const TEMPO_LANE_PREF_DEFAULTS: TempoLanePrefsState = {
	show_tempo_lane: true,
	tempo_lane_scale: 'fixed_10'
};

/** Narrow an unknown to a TempoLaneScale, else undefined. */
export function parseTempoLaneScale(value: unknown): TempoLaneScale | undefined {
	return TEMPO_LANE_SCALES.find((scale) => scale === value);
}

/** Validate the stored blob's tempo-lane fields: absent means a blob written
 * before the keys existed (caller keeps the default); present but malformed
 * throws with the storage key named, like every other pref. */
export function validateTempoLanePrefFields(
	parsed: { show_tempo_lane?: unknown; tempo_lane_scale?: unknown },
	storageKey: string
): Partial<TempoLanePrefsState> {
	const out: Partial<TempoLanePrefsState> = {};
	const show = parsed.show_tempo_lane;
	if (show !== undefined) {
		if (typeof show !== 'boolean') {
			throw new Error(
				`${storageKey}: malformed prefs blob (show_tempo_lane is not a boolean) - ` +
					'clear the localStorage key to recover'
			);
		}
		out.show_tempo_lane = show;
	}
	if (parsed.tempo_lane_scale !== undefined) {
		const scale = parseTempoLaneScale(parsed.tempo_lane_scale);
		if (scale === undefined) {
			throw new Error(
				`${storageKey}: malformed prefs blob (tempo_lane_scale is not ${TEMPO_LANE_SCALES.join('|')}) - ` +
					'clear the localStorage key to recover'
			);
		}
		out.tempo_lane_scale = scale;
	}
	return out;
}

export interface TempoLaneSetters {
	setShowTempoLane(next: boolean): void;
	setTempoLaneScale(next: TempoLaneScale): void;
}

/** Every setter writes localStorage AND PUTs /api/v1/ui-prefs, so an agent
 * driving the HTTP endpoint and an operator clicking the control land on
 * the same persisted state (agent parity). */
export function makeTempoLaneSetters(
	state: TempoLanePrefsState,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<TempoLanePrefsState>) => void
): TempoLaneSetters {
	return {
		setShowTempoLane(next: boolean): void {
			state.show_tempo_lane = next;
			persist();
			syncDiskPrefs({ show_tempo_lane: next });
		},
		setTempoLaneScale(next: TempoLaneScale): void {
			if (parseTempoLaneScale(next) === undefined) {
				throw new Error(
					`tempo_lane_scale must be ${TEMPO_LANE_SCALES.join('|')}, got ${String(next)}`
				);
			}
			state.tempo_lane_scale = next;
			persist();
			syncDiskPrefs({ tempo_lane_scale: next });
		}
	};
}
