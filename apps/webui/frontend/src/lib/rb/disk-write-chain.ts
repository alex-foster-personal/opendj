/**
 * Serializes async disk-pref writes onto one promise chain, generic over the
 * patch shape so every caller (prefs.svelte.ts's own setters, and through the
 * hook it hands down, the level-calibration setters) shares exactly one
 * queue instead of each keeping a local chain (issue #1578).
 *
 * Two writes in quick succession can otherwise land out of order: an older
 * response can resolve after a newer one and overwrite it on disk, so the
 * live tab looks right and a just-made change reappears reverted after
 * reload. Chaining makes the last call the last write. The caller's `put`
 * is expected to swallow its own errors (localStorage stays authoritative if
 * the daemon is down), so one success handler is enough to keep the chain
 * itself from ever rejecting.
 */
export function makeDiskWriteChain<Patch>(
	put: (patch: Patch) => Promise<void>
): (patch: Patch) => Promise<void> {
	let chain: Promise<void> = Promise.resolve();
	return function syncDiskPrefs(patch: Patch): Promise<void> {
		chain = chain.then(() => put(patch));
		return chain;
	};
}
