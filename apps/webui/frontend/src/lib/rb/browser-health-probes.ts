/** Actual browser/engine liveness probes and validated ingest coverage readout. */
import { pingHealth, timeoutSignal } from '$lib/rb/api-rb';
import type { IngestCoverage } from '$lib/rb/api-ingest';
import type { LibraryHealthDot } from '$lib/rb/library-health-dots';

const CONN_PING_TIMEOUT_MS = 2000;

export async function _pingBackend(): Promise<LibraryHealthDot> {
	try {
		await pingHealth(CONN_PING_TIMEOUT_MS);
		return { label: 'Backend', state: 'complete', detail: 'engine online' };
	} catch (error: unknown) {
		// The reason is kept rather than flattened to "offline": a timeout
		// and a 500 want different things from the reader.
		const why = error instanceof Error ? error.message : String(error);
		return { label: 'Backend', state: 'error', detail: `engine not answering - ${why}` };
	}
}

export async function _pingFrontend(): Promise<LibraryHealthDot> {
	const { signal, clear } = timeoutSignal(CONN_PING_TIMEOUT_MS);
	try {
		const response = await fetch(`${window.location.origin}/`, {
			method: 'GET',
			cache: 'no-store',
			signal
		});
		if (!response.ok) {
			return {
				label: 'Frontend',
				state: 'error',
				detail: `dev server returned HTTP ${response.status}`
			};
		}
		return { label: 'Frontend', state: 'complete', detail: 'dev server online' };
	} catch (error: unknown) {
		const why = error instanceof Error ? error.message : String(error);
		return { label: 'Frontend', state: 'error', detail: `dev server not answering - ${why}` };
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
