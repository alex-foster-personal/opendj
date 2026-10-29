/**
 * MIXGRP-01: a shift-press on a knob or fader is control selection, never the
 * strip's deck select. The strip listens for click, which fires after the
 * button comes up, so the click's own shiftKey is not enough: Shift can be let
 * go between pointerdown and pointerup. This remembers what the press was.
 */
export interface ShiftSelectPress {
	/** Call on every primary pointerdown with that event's shiftKey. */
	press(shiftKey: boolean): void;
	/** Call on click; true means stop it reaching the strip. Consumes the press. */
	click(shiftKey: boolean): boolean;
}

export function createShiftSelectPress(): ShiftSelectPress {
	let shiftPress = false;
	return {
		press(shiftKey) {
			shiftPress = shiftKey;
		},
		click(shiftKey) {
			const stop = shiftKey || shiftPress;
			shiftPress = false;
			return stop;
		}
	};
}
