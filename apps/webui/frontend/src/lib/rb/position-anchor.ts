/**
 * AGENT-30: a deck's playhead paired with the AudioContext time it was presented at, and when that pair may still be
 * projected forward.
 *
 * The class of staleness is "the deck's timeline mapping changed after the observation". Every transport change the
 * engine makes (play, pause, seek, tempo or pitch, loop on or off) acknowledges a new schedule revision on the deck's
 * presentation timeline, and a load or unload replaces that timeline object. So an anchor is valid only while it was
 * taken on the SAME timeline object at the SAME revision, with no newer revision pending, the deck playing, no loop
 * active (a wrap moves the playhead inside one revision) and the same master delay (it shifts what is presented).
 * Anything else answers null, and the plan executor then fires the step on its bar.
 */

export interface PositionAnchor {
	position_ms: number;
	context_sec: number;
	/** The tempo ratio the deck plays at on this timeline revision: track ms per context ms. */
	pitch: number;
}

/** What the anchor was taken against: the engine's own objects and values, compared by identity and equality. */
export interface AnchorBasis {
	timeline: object;
	revision: number;
	master_delay_ms: number;
}

export interface AnchorNow {
	timeline: object;
	desired_revision: number;
	presented_revision: number;
	playing: boolean;
	loop_active: boolean;
	master_delay_ms: number;
}

export interface RecordedAnchor extends PositionAnchor {
	basis: AnchorBasis;
}

/** The anchor if it still describes the deck's current timeline mapping, else null. */
export function validAnchor(
	recorded: RecordedAnchor | null,
	now: AnchorNow
): PositionAnchor | null {
	if (recorded === null) return null;
	const { basis } = recorded;
	const same =
		basis.timeline === now.timeline &&
		basis.revision === now.presented_revision &&
		now.desired_revision === now.presented_revision &&
		now.playing &&
		!now.loop_active &&
		basis.master_delay_ms === now.master_delay_ms;
	return same
		? {
				position_ms: recorded.position_ms,
				context_sec: recorded.context_sec,
				pitch: recorded.pitch
			}
		: null;
}
