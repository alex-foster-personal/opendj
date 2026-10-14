/**
 * CMDK-06: is a playing deck running out?
 *
 * The Cmd K bar's deck rings pulse red when a playing deck is within
 * RUNOUT_BEATS of its fade-out section (the outro phrase), or of the track's
 * end when the track has no outro phrase. the maintainer, Thu 8 Oct 2026: "If one is
 * running out (x beats from song-fade-out segment / end if not available)
 * then should pulse red."
 *
 * Pure and runeless so the unit suite loads it directly.
 */

/** Beats of warning before the outro (8 bars of 4/4). */
export const RUNOUT_BEATS = 32;

/** The phrase fields this needs (a slice of anlz-types AnlzPhrase). */
export interface RunoutPhrase {
	start_s: number;
	kind: number;
	mood: number;
	label?: string;
	source?: 'own';
}

/** The beat fields this needs (a slice of anlz-types AnlzBeat): time in seconds, local BPM. */
export interface RunoutBeat {
	t: number;
	bpm: number;
}

/** The deck fields this needs (a slice of DeckState). */
export interface RunoutDeck {
	stable_id: string | null;
	playing: boolean;
	position_ms: number;
	duration_ms: number | null;
	bpm: number | null;
	pitch: number;
	phrases: readonly RunoutPhrase[] | null;
	/** The deck's beat grid (ANLZ PQTZ or own), ordered by time. Null or empty when it has none. */
	beats?: readonly RunoutBeat[] | null;
}

/**
 * rekordbox PSSI phrase kinds that mean "outro": kind 6 under the high mood
 * (mood 1), kind 10 under the mid and low moods (2 and 3). Own sections carry
 * kind and mood 0 and name themselves in `label` instead.
 */
export function isOutroPhrase(p: RunoutPhrase): boolean {
	if (p.source === 'own') return /outro/i.test(p.label ?? '');
	if (p.mood === 1) return p.kind === 6;
	if (p.mood === 2 || p.mood === 3) return p.kind === 10;
	return false;
}

/**
 * Where the fade-out starts, in ms: the first phrase of the track's final run
 * of outro phrases (an outro can span several sections back to back), else the
 * end. Null when unknown.
 */
export function fadeOutStartMs(deck: Pick<RunoutDeck, 'duration_ms' | 'phrases'>): number | null {
	const phrases = [...(deck.phrases ?? [])].sort((a, b) => a.start_s - b.start_s);
	let i = phrases.length - 1;
	while (i >= 0 && !isOutroPhrase(phrases[i])) i -= 1;
	if (i >= 0) {
		while (i > 0 && isOutroPhrase(phrases[i - 1])) i -= 1;
		return phrases[i].start_s * 1000;
	}
	return deck.duration_ms !== null && deck.duration_ms > 0 ? deck.duration_ms : null;
}

/** Index of the first beat after `ms` (beats ordered by time). */
function firstBeatAfter(beats: readonly RunoutBeat[], ms: number): number {
	let lo = 0;
	let hi = beats.length;
	while (lo < hi) {
		const mid = (lo + hi) >> 1;
		if (beats[mid].t * 1000 <= ms) lo = mid + 1;
		else hi = mid;
	}
	return lo;
}

/**
 * Beats left before the fade-out, or null when the deck is not playing or
 * nothing is known about where it ends or its tempo (never a guess: an unknown
 * deck simply does not pulse).
 *
 * position_ms and the phrase and end times are all in the track's own
 * timeline, so the count is in the track's beats. With a beat grid it counts
 * the grid's beats between the playhead and the fade-out, so a track whose
 * tempo drifts or changes is counted beat by beat; past the grid's last beat
 * that beat's local BPM carries the rest. Without a grid, the deck's one BPM
 * converts the time left. Pitch changes how long those beats take to play,
 * not how many remain, so it does not enter the count.
 */
export function beatsUntilFadeOut(deck: RunoutDeck): number | null {
	if (deck.stable_id === null || !deck.playing) return null;
	const at = fadeOutStartMs(deck);
	if (at === null) return null;
	const beats = deck.beats ?? [];
	// Past the fade-out start the grid count goes zero or negative, so the ring
	// keeps pulsing without needing the scalar BPM.
	if (beats.length > 0) {
		const from = firstBeatAfter(beats, deck.position_ms);
		const to = firstBeatAfter(beats, at);
		const last = beats[beats.length - 1];
		const tailFrom = Math.max(deck.position_ms, last.t * 1000);
		const tail = at > tailFrom && last.bpm > 0 ? ((at - tailFrom) / 60000) * last.bpm : 0;
		return to - from + tail;
	}
	if (deck.bpm === null || !(deck.bpm > 0)) return null;
	const msLeft = at - deck.position_ms;
	return (msLeft / 60000) * deck.bpm;
}

/** True while a playing deck is inside its warning window or past the fade-out start. */
export function isRunningOut(deck: RunoutDeck, beats: number = RUNOUT_BEATS): boolean {
	const left = beatsUntilFadeOut(deck);
	return left !== null && left <= beats;
}
