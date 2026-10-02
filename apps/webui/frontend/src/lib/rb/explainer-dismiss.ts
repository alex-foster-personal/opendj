/**
 * Hover explainer dismiss categories (pin 894af5672c3b, JIK Wed 16 Sep 2026:
 * "quite a few of the hover explainers are taking 'stay if mouseover' ... it's
 * important to categorize these correctly").
 *
 * - `instant`: informational. The popover is click-through (pointer-events:
 *   none) and closes the moment the pointer leaves the trigger, so it can never
 *   block the control painted underneath it. Any action slot is shown only
 *   once the explainer is click-pinned, because an instant popover cannot be
 *   reached by the pointer.
 * - `stay`: interactive. The popover holds its own pointer events and stays
 *   open while the pointer is over it (with a short grace between trigger and
 *   popover), so the selects or buttons inside it can be used.
 *
 * Default when a caller does not say: interactive content (an action slot)
 * stays, everything else is instant.
 */
export type ExplainerDismiss = 'instant' | 'stay';

export function resolveExplainerDismiss(dismiss: ExplainerDismiss | undefined, hasAction: boolean): ExplainerDismiss {
	if (dismiss !== undefined) return dismiss;
	return hasAction ? 'stay' : 'instant';
}

/** Whether the action slot renders right now. */
export function explainerShowsAction(mode: ExplainerDismiss, hasAction: boolean, pinned: boolean): boolean {
	if (!hasAction) return false;
	return mode === 'stay' || pinned;
}

/** Whether the popover takes pointer events (an instant one is click-through until pinned). */
export function explainerPopInteractive(mode: ExplainerDismiss, pinned: boolean): boolean {
	return mode === 'stay' || pinned;
}
