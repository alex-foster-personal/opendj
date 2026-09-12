import { hasRealBeatGrid } from '$lib/player/grid-features';
import type { AnlzData } from '$lib/rb/anlz-types';

/** Whether the strip should show the measured native grid marker. */
export function shouldShowNativeGridMarker(anlz: AnlzData | null): boolean {
	if (anlz === null) return false;
	if (anlz.beatgrid.source !== 'own') return false;
	const status = anlz.beatgrid.status;
	if (status === 'missing' || status === 'failed') return false;
	return hasRealBeatGrid(anlz.beatgrid.beats);
}
