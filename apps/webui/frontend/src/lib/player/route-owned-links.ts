/**
 * Links the /performance route owns that live outside its audio graph (the
 * remote speaker's WebRTC peers). A lazily loaded module registers its closer
 * here once; the route's teardown runs every closer, so nothing keeps playing
 * or reads linked after the route is gone, and the route never has to import
 * the module itself.
 */
const _closers = new Set<() => void>();

/** Register `close` to run on every route teardown. It must be idempotent. */
export function closeWithPerformanceRoute(close: () => void): void {
	_closers.add(close);
}

/** Route teardown: run every closer; one that throws is reported, never fatal. */
export function closeRouteOwnedLinks(): void {
	for (const close of _closers) {
		try {
			close();
		} catch (error) {
			console.error('performance route teardown: a route-owned link did not close', error);
		}
	}
}
