/**
 * When a track is selected or loaded on /performance without a shown genre,
 * queue infer through POST /api/v1/genres/enqueue (issue #11310).
 *
 * Port-injected for unit tests: no BrowserPanel import.
 */
import { enqueueGenreInference } from '$lib/rb/api-genres';

let _inFlight: Promise<void> | null = null;

/** Coalesce duplicate attention bursts while one enqueue is in flight. */
export function requestGenreInferOnAttention(stable_ids: string[]): void {
	const ids = [...new Set(stable_ids)];
	if (ids.length === 0) return;
	if (_inFlight !== null) return;
	_inFlight = enqueueGenreInference(ids)
		.then(() => undefined)
		.catch(() => undefined)
		.finally(() => {
			_inFlight = null;
		});
}

/** Test-only reset. */
export function resetGenreInferOnAttentionForTests(): void {
	_inFlight = null;
}
