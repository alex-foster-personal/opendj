/**
 * PREFLIGHT-01's boot gate DATA store (issue #771).
 *
 * Holds the reactive state behind `GET /api/v1/preflight` and nothing else.
 * Polling POLICY (when and how often to re-check) deliberately lives in
 * `PreflightScreen.svelte`, which differs by mode: the boot gate polls only
 * while red and stops the moment it clears, while the admin reference
 * polls continuously for as long as it is mounted. This module holds one
 * shared verdict so both consumers read the identical answer -- never a
 * second UI-side computation of pass/fail, per this issue's own
 * agent-native-parity requirement.
 *
 * `overallStatus` starts as `'unknown'`, a UI-only sentinel for "no response
 * yet" that is distinct from the server's own `pass`/`fail`, which is the
 * ONLY vocabulary `PreflightOut.status` uses. `cleared` -- the boot gate's
 * only exit -- is false until a real `pass` has been observed, so a slow or
 * failed first request holds the gate exactly like a `fail` response would.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 `cleared` is true iff the last real response was `status: "pass"`.
 *     [if] `cleared` reads true before any response has arrived [then ⛔️] broken
 *   ✔︎ 🎯 a network failure holds the gate (never crashes it, never clears
 *     it): `error` carries the real message and `overallStatus` stays
 *     whatever it last was.
 *     [if] `checkPreflight()` throws out of this module [then ⛔️] broken
 */
import { type PreflightCheck, type PreflightResult, getPreflight } from '../api';

export type PreflightOverallStatus = 'unknown' | PreflightResult['status'];

/** The check id for PREFLIGHT-02's library-attached row. */
export const LIBRARY_ATTACHED_CHECK_ID = 'library-attached';

/** Whether the boot preflight screen should cover the whole app.
 *
 * While the first-run setup overlay is open the wizard is actively resolving
 * library-attached, so the boot gate must not sit on top of it. */
export function shouldBlockOnPreflight(
	status: PreflightOverallStatus,
	setupOpen: boolean
): boolean {
	if (setupOpen) return false;
	return status !== 'pass';
}

/** Which preflight rows should render. While setup is open the
 * library-attached row is hidden because the wizard is handling it. */
export function visiblePreflightChecks(
	checks: PreflightCheck[],
	setupOpen: boolean
): PreflightCheck[] {
	if (!setupOpen) return checks;
	return checks.filter((check) => check.id !== LIBRARY_ATTACHED_CHECK_ID);
}

let overallStatus = $state<PreflightOverallStatus>('unknown');
let checks = $state<PreflightCheck[]>([]);
let error = $state<string | null>(null);

function _applyResult(result: PreflightResult): void {
	overallStatus = result.status;
	checks = result.checks;
	error = null;
}

/** One GET, applied to state. Never throws: a caller polling on an interval
 * must not have that interval killed by an unhandled rejection, and the
 * boot screen must show the real network error rather than crash. */
export async function checkPreflight(): Promise<void> {
	try {
		_applyResult(await getPreflight());
	} catch (exc) {
		error = exc instanceof Error ? exc.message : String(exc);
	}
}

/** "Re-request permissions" IS "Re-check" -- see `getPreflight` in
 * `src/lib/api.ts`, which says why one GET is both. Exported
 * under its own name so the two buttons can each carry an honest label and
 * `title` without the caller having to know they are the same call. */
export const requestPermissions = checkPreflight;

/** Reset to the pre-boot sentinel. Test-only. */
export function _resetPreflightForTests(): void {
	overallStatus = 'unknown';
	checks = [];
	error = null;
}

export const preflightGate = {
	get status() {
		return overallStatus;
	},
	get checks() {
		return checks;
	},
	get error() {
		return error;
	},
	/** The boot gate's ONLY exit. Deliberately no skip/continue-anyway. */
	get cleared() {
		return overallStatus === 'pass';
	}
};
