/** Actual browser/engine liveness probes and validated ingest coverage readout. */
import { pingHealth, timeoutSignal } from '$lib/api';
import { noteEngineBoot } from '$lib/rb/autoplay-next-up-lazy';
import { CoverageTimeoutError, type IngestCoverage } from '$lib/rb/api-ingest';
import type { LibraryHealthDot } from '$lib/rb/library-health-dots';
import type { ProbeOutcome } from '$lib/rb/liveness-dots';

// Re-exported so the panel reaches the liveness policy through the probes
// module it already imports, adding no edge to its import fan-out.
export { appUiDot, coverageTimeoutDot, createLivenessTracker } from '$lib/rb/liveness-dots';

/** A coverage read the engine did not answer in time (HEALTH-8304c). */
export function isCoverageTimeout(error: unknown): boolean {
	return error instanceof CoverageTimeoutError;
}

const CONN_PING_TIMEOUT_MS = 2000;

/** True for the abort our own timeout signal raises. Chromium rejects with
 * the signal's TimeoutError reason; WebKit (the Tauri shell) rejects with an
 * AbortError whose message is "Fetch is aborted". Nothing else aborts these
 * probes, so both mean "no answer in time". */
export function isProbeTimeout(error: unknown): boolean {
	return error instanceof Error && (error.name === 'TimeoutError' || error.name === 'AbortError');
}

function _probeFailure(error: unknown): ProbeOutcome {
	if (isProbeTimeout(error)) {
		return { ok: false, timedOut: true, why: `no answer within ${CONN_PING_TIMEOUT_MS / 1000} s` };
	}
	// The reason is kept rather than flattened to "offline": a refused
	// connection and a 500 want different things from the reader.
	return { ok: false, timedOut: false, why: error instanceof Error ? error.message : String(error) };
}

export async function _pingBackend(): Promise<ProbeOutcome> {
	try {
		noteEngineBoot(await pingHealth(CONN_PING_TIMEOUT_MS));
		return { ok: true };
	} catch (error: unknown) {
		return _probeFailure(error);
	}
}

/** Dev only: in a packaged app the engine serves the page, see `appUiDot`. */
export async function _pingFrontend(): Promise<ProbeOutcome> {
	const { signal, clear } = timeoutSignal(CONN_PING_TIMEOUT_MS);
	try {
		const response = await fetch(`${window.location.origin}/`, {
			method: 'GET',
			cache: 'no-store',
			signal
		});
		if (!response.ok) return { ok: false, timedOut: false, why: `HTTP ${response.status}` };
		return { ok: true };
	} catch (error: unknown) {
		return _probeFailure(error);
	} finally {
		clear();
	}
}

export function _coverageDot(
	label: LibraryHealthDot['label'],
	coverage: IngestCoverage,
	step: 'vocals' | 'stems' | 'lyrics'
): LibraryHealthDot {
	const missing = coverage.missing[step];
	if (typeof missing !== 'number' || !Number.isInteger(missing) || missing < 0) {
		return { label, state: 'unavailable', detail: `${step} coverage could not be measured` };
	}
	if (coverage.on_disk <= 0) {
		return {
			label,
			state: 'unavailable',
			detail: `no playable tracks to measure, ${coverage.unreachable} broken ${coverage.unreachable === 1 ? 'link' : 'links'}`
		};
	}
	const completed = coverage.on_disk - missing;
	if (completed < 0) {
		throw new Error(`${step} coverage missing count exceeds on-disk tracks`);
	}
	// Corruption is a DISTINCT, always-surfaced state - never folded into a
	// quiet 'incomplete'. It is a subset of `missing` (a malformed entry is
	// not done, whatever else it is), so it is checked after validating
	// `missing` but before the ordinary complete/incomplete split. lyrics
	// has no refresh runner (see routes/ingest.py), so a corrupt lyrics
	// entry has NO repair path except this dot saying so.
	const corrupt = coverage.corrupt[step];
	if (typeof corrupt !== 'number' || !Number.isInteger(corrupt) || corrupt < 0) {
		throw new Error(`${step} coverage corrupt count must be a nonnegative integer`);
	}
	if (corrupt > 0) {
		return {
			label,
			state: 'error',
			detail: `${corrupt} corrupt ${corrupt === 1 ? 'entry' : 'entries'} - ${completed}/${coverage.on_disk} playable complete, ${missing} missing, ${coverage.unreachable} broken ${coverage.unreachable === 1 ? 'link' : 'links'}`
		};
	}
	return {
		label,
		state: missing === 0 ? 'complete' : 'incomplete',
		detail: `${completed}/${coverage.on_disk} playable complete, ${missing} missing, ${coverage.unreachable} broken ${coverage.unreachable === 1 ? 'link' : 'links'}`
	};
}
