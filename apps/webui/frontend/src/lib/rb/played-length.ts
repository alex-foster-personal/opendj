/**
 * Rough played length of a track selection (LIBUX played length).
 *
 * Mirror of apps/shared/played_length.py, which the HTTP route, CLI and MCP
 * tool use; tests/fixtures/played_length_cases.json pins both to the same
 * answers. Pure arithmetic over what the listing already carries, so it stays
 * instant for hundreds of selected rows.
 */

export const OVERLAP_BARS = 32;
const BEATS_PER_BAR = 4;
const MIN_PLAYED_FRACTION = 0.5;
const NO_BPM_FRACTION = 0.75;

export type PlayedLengthSource = 'mix_points' | 'rule' | 'unknown';

export interface PlayedLengthInput {
	duration_s: number | null;
	bpm: number | null;
	mix_in_s: number | null;
	mix_out_s: number | null;
}

export interface PlayedLength {
	played_s: number;
	total_s: number;
	from_mix_points: number;
	from_rule: number;
	unknown: number;
}

export function estimateTrack(track: PlayedLengthInput): [number, PlayedLengthSource] {
	const duration = track.duration_s;
	if (duration === null || duration <= 0) return [0, 'unknown'];
	if (track.mix_in_s !== null && track.mix_out_s !== null && track.mix_out_s > track.mix_in_s) {
		return [track.mix_out_s - track.mix_in_s, 'mix_points'];
	}
	if (track.bpm !== null && track.bpm > 0) {
		const overlap = (OVERLAP_BARS * BEATS_PER_BAR * 60) / track.bpm;
		return [Math.max(duration - overlap, MIN_PLAYED_FRACTION * duration), 'rule'];
	}
	return [NO_BPM_FRACTION * duration, 'rule'];
}

export function estimatePlayedLength(tracks: readonly PlayedLengthInput[]): PlayedLength {
	const result: PlayedLength = { played_s: 0, total_s: 0, from_mix_points: 0, from_rule: 0, unknown: 0 };
	for (const track of tracks) {
		const [seconds, source] = estimateTrack(track);
		result.played_s += seconds;
		if (source === 'mix_points') result.from_mix_points += 1;
		else if (source === 'rule') result.from_rule += 1;
		else result.unknown += 1;
		if (source !== 'unknown' && track.duration_s !== null) result.total_s += track.duration_s;
	}
	return result;
}

/** "42m" under an hour, "1h 05m" from an hour up. */
export function formatPlayedLength(seconds: number): string {
	const minutes = Math.round(seconds / 60);
	if (minutes < 60) return `${minutes}m`;
	return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}
