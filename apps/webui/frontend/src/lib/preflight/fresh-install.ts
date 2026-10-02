/**
 * First-run orchestration helpers (issue #2722). Pure predicates extracted
 * from +layout.svelte so the preflight-empty-library path is unit-testable.
 */
import type { PreflightCheck } from '../api';

export const ENGINE_ALIVE_CHECK_ID = 'engine-alive';
export const LIBRARY_ATTACHED_CHECK_ID = 'library-attached';

function checkStatus(
	checks: readonly PreflightCheck[],
	id: string
): PreflightCheck['status'] | 'unknown' {
	return checks.find((check) => check.id === id)?.status ?? 'unknown';
}

/** True when the engine is healthy but the library row says import is needed. */
export function needsSetupForEmptyLibrary(
	checks: readonly PreflightCheck[],
	setupOpen: boolean
): boolean {
	if (setupOpen) return false;
	if (checkStatus(checks, ENGINE_ALIVE_CHECK_ID) !== 'pass') return false;
	const library = checkStatus(checks, LIBRARY_ATTACHED_CHECK_ID);
	// 'pending' is NOT a second flavour of 'fail'. On library-attached the engine
	// returns it in exactly two places, and both say "setup was dismissed" (an
	// empty library with no state.db, and one with 0 tracks). There is no
	// still-loading 'pending' on this check, so treating it as a reason to raise
	// the wizard made dismissal change the engine's answer without changing the
	// outcome: "Continue without importing" closed the overlay, this predicate
	// immediately asked for it back, and reopening cleared the incomplete flag
	// that would have explained why. The escape hatch was unreachable on exactly
	// the fresh install it exists for.
	return library === 'fail';
}

/**
 * Whether the root layout may raise setup because the library row is a
 * blocking fail.
 *
 * `operatorClosedSetup` is the in-tab latch set when the operator closes the
 * wizard. Preflight can still say `library-attached: fail` until its next
 * poll, and reopening on that stale row is what makes "Skip for now" and
 * "Start playing" bounce straight back (issue #3422).
 */
export function shouldAutoOpenEmptyLibrarySetup(
	checks: readonly PreflightCheck[],
	setupOpen: boolean,
	operatorClosedSetup: boolean
): boolean {
	if (operatorClosedSetup) return false;
	return needsSetupForEmptyLibrary(checks, setupOpen);
}
