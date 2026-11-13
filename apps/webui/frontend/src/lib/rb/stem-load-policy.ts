/**
 * Pure stem load policy: precomputed bundle preference before live-generation fallback.
 *
 * PERFBATCH-06 / LATENCY-04:
 *   [if] probe is ready [then] use precomputed decode; never consider live gen [else stop]
 *   [if] probe is hydrating [then] await precomputed; never consider live gen [else stop]
 *   [if] probe is unavailable [then] may invoke live-generation fallback once [else stop]
 *   [if] probe is invalid or deck load is stale (null probe) [then] reject; never fallback [else stop]
 */

/** Settled probe inputs for stem load policy (mirrors api-rb StemArtifactProbe). */
export type StemLoadProbeInput =
	| { status: 'ready' }
	| { status: 'hydrating' }
	| { status: 'unavailable'; error: string }
	| { status: 'invalid'; error: string };

export type StemLoadPolicyDecision =
	| 'use-precomputed'
	| 'await-precomputed'
	| 'consider-live-generation-fallback'
	| 'reject-loud'
	| 'aborted-stale';

export type StemLiveGenerationFallback = () => boolean;

/** Prefer a fresh precomputed bundle; live-generation only when probe is unavailable. */
export function resolveStemLoadPolicy(
	probe: StemLoadProbeInput | null,
	considerLiveGeneration: StemLiveGenerationFallback
): StemLoadPolicyDecision {
	if (probe === null) {
		return 'aborted-stale';
	}
	switch (probe.status) {
		case 'ready':
			return 'use-precomputed';
		case 'hydrating':
			return 'await-precomputed';
		case 'invalid':
			return 'reject-loud';
		case 'unavailable':
			considerLiveGeneration();
			return 'consider-live-generation-fallback';
		default: {
			const _exhaustive: never = probe;
			throw new Error(`Unhandled stem load probe: ${JSON.stringify(_exhaustive)}`);
		}
	}
}
