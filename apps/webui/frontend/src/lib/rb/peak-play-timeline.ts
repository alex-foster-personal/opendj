/** Peak play timeline helper (AI-05, issue #342). Pure, no player imports. */

export const PEAK_TIMELINE_CAP = 12;

export function pushPlayed(timeline: readonly string[], playedId: string): string[] {
	if (timeline.length > 0 && timeline[timeline.length - 1] === playedId) {
		return [...timeline];
	}
	const next = [...timeline, playedId];
	if (next.length <= PEAK_TIMELINE_CAP) {
		return next;
	}
	return next.slice(next.length - PEAK_TIMELINE_CAP);
}
