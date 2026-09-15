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
import { type SetupStatus, finalSetupRefusal, getSetupStatus } from './setup-api';

export const FIRST_RUN_PROBE_TIMEOUT_MS = 5_000;
export const FIRST_RUN_PROBE_INTERVAL_MS = 250;

export interface FirstRunResult {
	show: boolean;
	error: string | null;
}

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

function _sleep(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Poll until the daemon flavor is no longer unknown, or timeout. */
export async function waitForEngineFlavor(
	timeoutMs = FIRST_RUN_PROBE_TIMEOUT_MS,
	intervalMs = FIRST_RUN_PROBE_INTERVAL_MS
): Promise<'engine' | 'legacy' | 'timeout'> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		const flavor = await capabilities.probe();
		if (flavor !== 'unknown') return flavor;
		await _sleep(intervalMs);
	}
	return 'timeout';
}

/**
 * Probe the daemon and resolve the gate. Returns false rather than throwing
 * for status-fetch failures (show the library). Probe timeout is NOT silent:
 * resolveFirstRunWithMeta surfaces it as an error for the boot gate.
 */
export async function resolveFirstRunWithMeta(
	options: { probeTimeoutMs?: number; probeIntervalMs?: number } = {}
): Promise<FirstRunResult> {
	const flavor = await waitForEngineFlavor(
		options.probeTimeoutMs ?? FIRST_RUN_PROBE_TIMEOUT_MS,
		options.probeIntervalMs ?? FIRST_RUN_PROBE_INTERVAL_MS
	);
	if (flavor === 'timeout') {
		return {
			show: false,
			error:
				'Could not reach the app engine in time. Check that the backend is running, then retry.'
		};
	}
	const refusal = finalSetupRefusal();
	if (refusal !== null) return { show: false, error: null };
	try {
		return { show: shouldShowFirstRun(refusal, await getSetupStatus()), error: null };
	} catch (exc) {
		console.error('[library] first-run check failed', exc);
		return { show: false, error: null };
	}
}

export async function resolveFirstRun(): Promise<boolean> {
	const result = await resolveFirstRunWithMeta();
	return result.show;
}
