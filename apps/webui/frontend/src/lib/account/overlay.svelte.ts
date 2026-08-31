/**
 * Open / closed state for the ACCOUNT OVERLAY.
 *
 * WHY AN OVERLAY, NOT A SETTINGS ROW. Account management is a stateful flow --
 * you arrive, read what is stored about you, and may take an irreversible
 * action -- not a flat searchable preference. The settings catalog is the
 * wrong shape for that (a pref toggles; this one deletes a row and signs you
 * out). So it follows the SetupOverlay precedent: a panel drawn OVER the live
 * app, mounted once from the root layout, raised through ONE function that
 * every door calls, exactly as `runSetup()` is reached from the Cmd+, palette,
 * /admin and /settings.
 *
 * ONE DOOR TODAY, the user bauble menu, which until now offered only
 * "Sign out". More doors are cheap precisely because they all call
 * `openAccountOverlay()` rather than each poking at their own flag.
 *
 * AGENT-NATIVE PARITY. Nothing here is daemon state. These two booleans are
 * how this tab is DRAWING the panel; every fact it shows and every action it
 * offers is an HTTP call (`GET /api/v1/account`, `DELETE /api/v1/account`,
 * `POST /api/v1/auth/logout`). An agent drives the same flow with curl and
 * never touches this module.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 openAccountOverlay always opens expanded and clears any stale
 *     confirmation, so a panel reopened after a cancelled delete never
 *     reappears mid-confirm.
 *     [if] reopening lands on the confirm step [then ⛔️] broken
 *   ✔︎ 🎯 closeAccountOverlay leaves nothing on screen.
 *     [if] `open` survives a close [then ⛔️] broken
 */

let open = $state(false);
/** True once the user has asked to delete and not yet confirmed. */
let confirmingDelete = $state(false);

/** THE way in. Every door calls this one function. */
export function openAccountOverlay(): void {
	open = true;
	confirmingDelete = false;
}

export function closeAccountOverlay(): void {
	open = false;
	confirmingDelete = false;
}

/** Arm the typed confirmation for the destructive action. */
export function askAccountDeleteConfirm(): void {
	confirmingDelete = true;
}

/** Stand down without deleting anything. */
export function cancelAccountDeleteConfirm(): void {
	confirmingDelete = false;
}

/** Reactive snapshot for components. THE read side: nothing else exports a
 * getter per flag, so a component cannot read one of these two through a path
 * the other does not have. */
export const accountOverlay = {
	get open() {
		return open;
	},
	get confirmingDelete() {
		return confirmingDelete;
	}
};
