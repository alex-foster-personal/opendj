/**
 * LIBUX-02: session-only collapse state for the Next / Recommended library
 * panels (`SuggestNextStrip` + `RecommendedSection`, mounted below the
 * track table in `BrowserPanel.svelte`).
 *
 * ONE combined toggle governs both panels together, per the requirement text
 * ("the > chevron on the LHS of the Next / recommended panels is clicked
 * [then] the panels collapse into a < chevron on the RHS") -- a single
 * chevron, plural panels moving as a unit.
 *
 * Deliberately NOT persisted anywhere (no localStorage, no disk sync): the
 * requirement is explicit that a relaunch always restores both panels
 * expanded, so this is plain module-scope $state, gone the moment the page
 * reloads. Do not route this through `prefs.svelte.ts` -- that module exists
 * specifically for state that DOES survive a relaunch, and adding a
 * non-persisted field there would be a second, contradictory meaning for the
 * same file.
 *
 * Programmatic parity (house rule: every UI control has a non-UI path):
 * - isLibraryPanelsCollapsed() / setLibraryPanelsCollapsed(bool) /
 *   toggleLibraryPanels() for in-app callers.
 * - window.__mdtLibraryPanels for a headless test agent driving the page
 *   from the outside, where the ES module scope is unreachable (same shape
 *   as the existing __mdtMasterMute / __mdtWheelSensitivity bridges).
 *
 * Regression lines:
 * - if the module starts collapsed then every session opens with the panels
 *   already hidden
 * - if noteVisibleLibraryRowCount does not force a collapse the moment the
 *   row count first drops under MIN_VISIBLE_ROWS_FOR_AUTO_COLLAPSE then the
 *   auto-collapse acceptance criterion never fires
 * - if it keeps forcing a collapse on every call while still under the floor
 *   then a manual re-expand is fought back closed immediately
 * - if setLibraryPanelsCollapsed accepts a non-boolean then a stray value
 *   wedges the panels in an inconsistent state
 */

/** Library rows visible in the active pane below which the panels free their
 * vertical space automatically. Matches BrowserPanel's `visibleRows.length`. */
export const MIN_VISIBLE_ROWS_FOR_AUTO_COLLAPSE = 10;

let _collapsed = $state(false);

/** Edge-triggered latch: true only once the row count has actually crossed
 * below the floor, so noteVisibleLibraryRowCount can tell a fresh crossing
 * from "still under the floor" and not fight a manual re-expand every call. */
let _wasBelowFloor = false;

export function isLibraryPanelsCollapsed(): boolean {
	return _collapsed;
}

export function setLibraryPanelsCollapsed(collapsed: boolean): void {
	if (typeof collapsed !== 'boolean') {
		throw new TypeError('setLibraryPanelsCollapsed: collapsed must be boolean');
	}
	_collapsed = collapsed;
}

export function toggleLibraryPanels(): void {
	setLibraryPanelsCollapsed(!_collapsed);
}

/** Call reactively with the active pane's current visible row count. Forces
 * a collapse the moment the count crosses below the floor; does nothing on
 * every other call. */
export function noteVisibleLibraryRowCount(count: number): void {
	if (typeof count !== 'number' || !Number.isFinite(count) || count < 0) {
		throw new TypeError('noteVisibleLibraryRowCount: count must be a non-negative finite number');
	}
	const belowFloor = count < MIN_VISIBLE_ROWS_FOR_AUTO_COLLAPSE;
	if (belowFloor && !_wasBelowFloor) _collapsed = true;
	_wasBelowFloor = belowFloor;
}

//-----------------------------------------------------------------------------
// out-of-page bridge
//-----------------------------------------------------------------------------

/** `window.__mdtLibraryPanels` for headless agents: `.get()` / `.set(bool)`.
 * Installed once at module load; browser only. */
export function installLibraryPanelsGlobal(): void {
	if (typeof window === 'undefined') return;
	const w = window as Window & {
		__mdtLibraryPanels?: { get: () => boolean; set: (collapsed: boolean) => void };
	};
	w.__mdtLibraryPanels = {
		get: () => isLibraryPanelsCollapsed(),
		set: (collapsed) => setLibraryPanelsCollapsed(collapsed)
	};
}

installLibraryPanelsGlobal();
