/**
 * Horizontal wheel routing for selected mixer channels (MIXUX-08 AC7).
 */
import { horizontalWheelDirection } from './wheel-adjust';
import type { HorizontalWheelKnob } from './prefs.svelte';

export function shouldRouteHorizontalWheel(event: WheelEvent, target: EventTarget | null): boolean {
	if (horizontalWheelDirection(event) === 0) return false;
	if (!(target instanceof Element)) return true;
	if (target.closest('[data-library-root]') !== null) return false;
	return true;
}

export function knobCommandType(pref: HorizontalWheelKnob): 'filter' {
	// Color FX dial is not built; both prefs adjust FILTER for now.
	return 'filter';
}
