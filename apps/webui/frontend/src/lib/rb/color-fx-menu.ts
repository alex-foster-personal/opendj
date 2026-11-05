/**
 * Color FX options menu state (COLORFX-10), kept pure so the gesture rules are
 * unit-testable without a DOM.
 *
 * Rules:
 * - A tap on the color knob (dial or caption, press without drag) opens the menu;
 *   a tap while it is open closes it.
 * - Any pointerdown outside the menu closes it, and the press carries on to
 *   whatever is under it: the document listener only reads the event, it never
 *   calls preventDefault or stopPropagation, so a drag on another knob starts
 *   in the same gesture. A native <select> could not do this (its popup eats
 *   the closing click), which is why the menu is our own element.
 * - A press inside the menu leaves it open for the option's click.
 */

/** Where a pointerdown landed, relative to one strip's color FX controls. */
export type ColorFxPressTarget = 'menu' | 'trigger' | 'outside';

/** What the next tap does, decided at the press that started it. */
export type ColorFxTapIntent = 'open' | 'stay-closed' | 'toggle';

export interface ColorFxMenuState {
	open: boolean;
	/** Set by the press; `toggle` = no pointer press seen (keyboard Enter/Space). */
	tapIntent: ColorFxTapIntent;
}

export const COLOR_FX_MENU_CLOSED: ColorFxMenuState = Object.freeze({ open: false, tapIntent: 'toggle' });

/** Document capture-phase pointerdown. Pure: the caller must not touch the event. */
export function colorFxMenuOnPointerDown(state: ColorFxMenuState, where: ColorFxPressTarget): ColorFxMenuState {
	switch (where) {
		case 'menu':
			return state;
		case 'trigger':
			// The press that closes an open menu must not reopen it on release.
			return { open: false, tapIntent: state.open ? 'stay-closed' : 'open' };
		case 'outside':
			return { open: false, tapIntent: 'toggle' };
		default: {
			const _exhaustive: never = where;
			throw new Error(`Unhandled color FX press target: ${_exhaustive}`);
		}
	}
}

/** Knob tap (pointerup inside the click slop, or Enter / Space). */
export function colorFxMenuOnTap(state: ColorFxMenuState): ColorFxMenuState {
	switch (state.tapIntent) {
		case 'open':
			return { open: true, tapIntent: 'toggle' };
		case 'stay-closed':
			return { open: false, tapIntent: 'toggle' };
		case 'toggle':
			return { open: !state.open, tapIntent: 'toggle' };
		default: {
			const _exhaustive: never = state.tapIntent;
			throw new Error(`Unhandled color FX tap intent: ${_exhaustive}`);
		}
	}
}

/** Option chosen, or Escape: the menu closes. */
export function colorFxMenuClose(): ColorFxMenuState {
	return COLOR_FX_MENU_CLOSED;
}

/** Classify a pointerdown target against the strip's menu and trigger roots. */
export function classifyColorFxPress(
	target: Node | null,
	menu: Node | null,
	trigger: Node | null
): ColorFxPressTarget {
	if (target !== null && menu !== null && menu.contains(target)) return 'menu';
	if (target !== null && trigger !== null && trigger.contains(target)) return 'trigger';
	return 'outside';
}
