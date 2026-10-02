/**
 * Setup reads under a deadline, and the finish() confirmation, split out of
 * wizard.svelte.ts to keep that module under the frontend file-size ratchet.
 * Plain TypeScript (no runes), so the node:test harness bundles it as is.
 */

import { LIBRARY_ATTACHED_CHECK_ID, needsSetupForEmptyLibrary } from '../preflight/fresh-install';
import { preflightGate } from '../preflight/preflight.svelte';
import { humanFinishLibraryMissing, humanFinishUnconfirmed } from './present';

export function errorMessage(exc: unknown): string {
	return exc instanceof Error ? exc.message : String(exc);
}

/** How long one setup read (status, detection) may take before the wizard
 * stops waiting and says the search did not finish. Detection on a large
 * collection answers in well under a second; 20 s is a stall, not a slow disk. */
export const SETUP_READ_TIMEOUT_MS = 20_000;

/** Thrown when a read hits its deadline. Carries the endpoint for agents. */
export class SetupReadTimeout extends Error {}

/** Run one read under a deadline: abort the request and reject when it passes.
 * Raced as well as aborted, so a fetch that ignores its signal still settles. */
export async function bounded<T>(
	what: string,
	ms: number,
	read: (signal: AbortSignal) => Promise<T>
): Promise<T> {
	const controller = new AbortController();
	let timer: ReturnType<typeof setTimeout> | undefined;
	const deadline = new Promise<never>((_, reject) => {
		timer = setTimeout(() => {
			controller.abort();
			reject(new SetupReadTimeout(`${what} gave no answer within ${ms} ms; aborted`));
		}, ms);
	});
	try {
		return await Promise.race([read(controller.signal), deadline]);
	} finally {
		clearTimeout(timer);
	}
}

/**
 * Why finish() must not close yet, read from the preflight just re-read, or
 * null when the engine agrees the library no longer needs setup. `human` is
 * the operator sentence, `diagnostic` the raw reason for agents.
 */
export function finishBlocker(): { human: string; diagnostic: string } | null {
	if (preflightGate.error !== null) {
		return {
			human: humanFinishUnconfirmed(),
			diagnostic:
				'setup was saved, but the startup checks could not be re-read ' +
				`(GET /api/v1/preflight), so setup cannot tell whether to close: ${preflightGate.error}`
		};
	}
	if (needsSetupForEmptyLibrary(preflightGate.checks, false)) {
		const row = preflightGate.checks.find((check) => check.id === LIBRARY_ATTACHED_CHECK_ID);
		return {
			human: humanFinishLibraryMissing(),
			diagnostic:
				'setup was saved, but the engine still reports no library attached ' +
				`(${row?.detail ?? 'no detail given'}), so closing would only reopen setup`
		};
	}
	return null;
}
