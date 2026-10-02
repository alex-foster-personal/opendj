/**
 * Open / collapsed / incomplete state for the first-run SETUP OVERLAY.
 *
 * WHY AN OVERLAY STORE AT ALL. Setup used to be a ROUTE (/setup). A route is
 * the wrong shape for this: it navigates a brand new user away from the app
 * they just launched, and it cannot be minimised while a 10,000-track import
 * runs. The overlay hosts the identical wizard steps ON TOP of the live
 * performance view, so the thing setup is about stays visible behind the ask.
 *
 * Mounted from the ROOT layout, exactly like $lib/settings/overlay.svelte, so
 * it works on /performance (which bypasses the app shell) as well as on the
 * shell routes.
 *
 * AGENT-NATIVE PARITY. Nothing here is engine state. `open`, `collapsed` and
 * `incomplete` are how this tab is DRAWING the setup surface; every decision
 * the operator can make through it is an HTTP call in $lib/setup/setup-api
 * (status, detect/rekordbox, detect/folder, import, import/folder, dismiss).
 * An agent drives the same flow with curl and never needs these three flags.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 collapse never closes: a collapsed overlay is still open, so a
 *     running import keeps its progress and reopens with it intact.
 *     [if] collapseSetupOverlay() clears `open` [then ⛔️] broken
 *   ✔︎ ✅ 🎯 reopening always clears the collapsed and incomplete flags, so the
 *     chip and the panel can never both claim the screen.
 *     [if] openSetupOverlay() leaves collapsed true [then ⛔️] broken
 *   ✔︎ ✅ 🎯 dismissing WITHOUT an import closes into an honest incomplete
 *     state rather than into silence.
 *     [if] closeSetupOverlay({ incomplete: true }) leaves no trace on screen
 *     [then ⛔️] broken
 *   ✔︎ 🎯 an explicit close holds the empty-library auto-open until the
 *     operator asks for setup again. Preflight's previous `fail` is still
 *     on screen for one poll, and reopening on it bounces Skip and Start
 *     playing (issue #3422).
 *     [if] closeSetupOverlay() leaves holdEmptyReopen false [then ⛔️] broken
 */

let open = $state(false);
let collapsed = $state(false);
let incomplete = $state(false);
/** Operator closed the wizard this tab. Blocks the stale-fail auto-open. */
let holdEmptyReopen = $state(false);

export function isSetupOverlayOpen(): boolean {
	return open;
}

export function isSetupOverlayCollapsed(): boolean {
	return collapsed;
}

export function isSetupIncomplete(): boolean {
	return incomplete;
}

/** Open (or re-open) the panel. Always expanded, never inheriting a chip. */
export function openSetupOverlay(): void {
	open = true;
	collapsed = false;
	incomplete = false;
	holdEmptyReopen = false;
}

/**
 * Close the panel.
 *
 * `incomplete` is the "the operator walked away without importing" flag. It
 * is NOT inferred here -- the caller knows whether an import ran, and guessing
 * from a track count would be a second truth about one decision.
 */
export function closeSetupOverlay(options: { incomplete?: boolean } = {}): void {
	open = false;
	collapsed = false;
	incomplete = options.incomplete === true;
	holdEmptyReopen = true;
}

/** Shrink to the progress chip. Still OPEN: the import keeps running and the
 * wizard keeps its step, which is the whole point of a chip over a close. */
export function collapseSetupOverlay(): void {
	if (!open) return;
	collapsed = true;
}

/** Back from the chip to the panel, with whatever the wizard was showing. */
export function expandSetupOverlay(): void {
	open = true;
	collapsed = false;
}

/** Drop the incomplete banner without re-opening anything. */
export function clearSetupIncomplete(): void {
	incomplete = false;
}

/** Drop everything, for tests. */
export function _resetSetupOverlayForTests(): void {
	open = false;
	collapsed = false;
	incomplete = false;
	holdEmptyReopen = false;
}

/** Reactive snapshot for components. */
export const setupOverlay = {
	get open() {
		return open;
	},
	get collapsed() {
		return collapsed;
	},
	get incomplete() {
		return incomplete;
	},
	get holdEmptyReopen() {
		return holdEmptyReopen;
	}
};
