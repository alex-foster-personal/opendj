/**
 * Which rationale tags earn their line on a NEXT tile.
 *
 * Pin 407a1601defe (the maintainer, Wed 2 Sep 2026): the bpm / camelot labels "are doing
 * no work. Save the vertical space." The tile's meta row already prints BPM,
 * key and energy, and the tile is only in the NEXT list because those matched,
 * so those tags restate the line above them and cost a whole row on a strip
 * where vertical space is the scarce resource.
 *
 * Deny-list, not allow-list, on purpose: a tag the engine starts emitting
 * later must reach the screen rather than being silently swallowed by a filter
 * nobody remembers to update. Only the tags known to be redundant are named.
 */

/** Tags whose information is already on the tile's meta row. */
const REDUNDANT_TAGS: ReadonlySet<string> = new Set([
	'bpm_match',
	'bpm_close',
	'camelot_step_0',
	'camelot_step_1_2',
	'energy_match'
]);

export function visibleRationaleTags(tags: readonly string[]): string[] {
	return tags.filter((t) => !REDUNDANT_TAGS.has(t));
}
