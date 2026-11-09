/**
 * TRYER-9999: a try-er lanes body must carry every curve the lanes draw.
 *
 * The lanes route promises a fader, low-EQ and filter curve for each deck. A curve the
 * server left out must read as missing data, never as an empty line that looks like
 * "this knob did not move". Pure, so it is unit-tested without a canvas.
 */
import type { TryLanes } from '$lib/api/dj-try';
import { LANE_KNOBS } from './tryer-lanes';

/** The failure text naming the first deck and curve that is missing, or null when every deck has all of them. */
export function missingLaneCurve(lanes: Pick<TryLanes, 'decks'>): string | null {
	for (const deck of lanes.decks) {
		for (const knob of LANE_KNOBS) {
			if (!Array.isArray(deck.curves[knob.name])) {
				return `deck ${deck.deck} has no ${knob.name} curve`;
			}
		}
	}
	return null;
}
