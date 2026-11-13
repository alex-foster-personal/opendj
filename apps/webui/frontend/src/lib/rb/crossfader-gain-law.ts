/**
 * The engine's equal-power crossfader law (issue #12486): the gain `_applyCrossfader` writes to
 * each deck's crossfader node. THRU is unity; A and B are cosine halves, -3.01 dB each at centre.
 */
import type { CrossfaderAssign } from '$lib/rb/mixer-types';

/** Equal-power crossfade gain for one bus assignment at position x (0..1). */
export function crossfaderGainFor(assign: CrossfaderAssign, x: number): number {
	if (assign === 'THRU') return 1;
	if (assign === 'A') return Math.cos((x * Math.PI) / 2);
	return Math.cos(((1 - x) * Math.PI) / 2);
}
