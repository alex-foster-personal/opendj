/**
 * The first-run gate: should the library page dim itself and offer setup?
 *
 * Split out of +page.svelte so the decision is executable without a DOM. The
 * page then has one job -- render an overlay while this says true -- and the
 * rule itself is under test rather than under a component mount.
 *
 * WHY AN OVERLAY AND NOT A REDIRECT. The gate used to `goto('/setup')`, which
 * navigated a brand new user away from the app before they had seen it. The
 * overlay leaves the library visible underneath, so the ask arrives with the
 * thing it is about behind it.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 the verdict is the daemon's `should_show_wizard`, never a rule
 *     assembled in the browser. The engine already folds in dev-mode (a repo
 *     checkout does not auto-trigger setup); a second rule here could
 *     disagree with it.
 *     [if] this module branches on dev_mode, library_empty or dismissed
 *     [then ⛔️] broken
 *   ✔︎ ✅ 🎯 a refused setup surface never asks. A legacy daemon has no
 *     /api/v1/setup, so the request is a guaranteed 404 that teaches nobody
 *     anything.
 *     [if] a legacy boot issues a status request [then ⛔️] broken
 *   ✔︎ ✅ 🎯 a status call that fails shows the LIBRARY. The tracks loaded
 *     fine; stranding the user behind a dialog over a working page because a
 *     side probe failed is the worse outcome.
 *     [if] a 500 from /setup/status raises the overlay [then ⛔️] broken
 */

import { capabilities } from '../api/capabilities.svelte';
import { type SetupStatus, getSetupStatus, setupRefusal } from './setup-api';

/**
 * The decision, given what is already known. Pure: no fetch, no navigation.
 *
 * `status` is null when there is nothing to go on -- not asked, or the ask
 * failed -- and the answer to "should I interrupt this user" with no
 * evidence is no.
 */
export function shouldShowFirstRun(
	refusal: string | null,
	status: SetupStatus | null
): boolean {
	if (refusal !== null) return false;
	if (status === null) return false;
	return status.should_show_wizard;
}

/**
 * Probe the daemon and resolve the gate. Returns false rather than throwing:
 * every failure mode here means "show the library", and a caller that had to
 * handle an exception would only translate it back into that same answer.
 */
export async function resolveFirstRun(): Promise<boolean> {
	await capabilities.probe();
	const refusal = setupRefusal();
	if (refusal !== null) return false;
	try {
		return shouldShowFirstRun(refusal, await getSetupStatus());
	} catch (exc) {
		console.error('[library] first-run check failed', exc);
		return false;
	}
}
