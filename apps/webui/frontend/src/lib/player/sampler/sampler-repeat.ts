/**
 * Per-tab repeat toggle (the maintainer, Tue 10 Nov): [1] once -> whole saved loop -> 4, 2, 8 beats -> [1].
 *
 * Requirements (mini-PRD):
 *   ✔︎ five clicks return to the start
 *     [if] nextRepeat is applied five times from 'once' [then] it is 'once' again [else ⛔️]
 *   ✔︎ a numeric repeat loops exactly that many master beats, never longer than the region
 *     [if] repeat is 8 on a 1-bar (4-beat) region [then] it loops 4 beats [else ⛔️]
 */

/** Mirrors sampler_models.Repeat on the server. */
export type Repeat = 'once' | 'loop' | 4 | 2 | 8;
export const REPEAT_CYCLE: readonly Repeat[] = ['once', 'loop', 4, 2, 8];

/** The repeat one click of the toggle moves to. */
export function nextRepeat(r: Repeat): Repeat {
	return REPEAT_CYCLE[(REPEAT_CYCLE.indexOf(r) + 1) % REPEAT_CYCLE.length];
}

/** Default by source: a saved loop starts looping, a plain cue plays once. */
export function defaultRepeat(isLoop: boolean): Repeat {
	return isLoop ? 'loop' : 'once';
}

/** Beats the voice plays per pass, or null for play-once of the whole region. */
export function loopBeats(r: Repeat, regionBeats: number): number | null {
	if (r === 'once') return null;
	if (r === 'loop') return regionBeats;
	return Math.min(r, regionBeats);
}

/** The toggle's tooltip for this repeat. */
export function repeatTip(r: Repeat, regionBars: number): string {
	if (r === 'once') return 'Play once: plays the region one time, then stops';
	if (r === 'loop')
		return `Loop the whole saved loop (${regionBars} ${regionBars === 1 ? 'bar' : 'bars'})`;
	return `Loop the first ${r} beats`;
}
