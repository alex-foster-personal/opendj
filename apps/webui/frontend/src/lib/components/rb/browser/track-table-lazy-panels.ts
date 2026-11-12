// First-use loading for the track table's works modals and version stack
// panel (#10272, PERF-BOOT-02). Plain TS so node tests can drive the replay
// and the null gate without mounting Svelte.

/** One dynamic import per document: the first call starts it, later calls share it.
 *  A rejection is kept, not retried (the browser's module map keeps a failed chunk
 *  failed, see QuickDrawMenuLoader), and its message is exposed for the inline error. */
export interface FirstUseImport<M> {
	load(): Promise<M>;
	readonly started: boolean;
	readonly failure: string | null;
}

export function firstUseImport<M>(importer: () => Promise<M>): FirstUseImport<M> {
	let promise: Promise<M> | null = null;
	let failure: string | null = null;
	return {
		load(): Promise<M> {
			if (promise === null) {
				promise = importer().catch((exc: unknown) => {
					failure = exc instanceof Error ? exc.message : String(exc);
					throw exc;
				});
			}
			return promise;
		},
		get started(): boolean {
			return promise !== null;
		},
		get failure(): string | null {
			return failure;
		}
	};
}

/** The WorksModals methods TrackTable calls through its bind:this handle. */
export interface WorksModalsMethods {
	stackTracks(ids: string[], title: string): Promise<void>;
}

/** Forwards each method: waits for the live instance (import + mount), then
 *  calls the same method with the same arguments. Calls made before the module
 *  lands are all replayed, in order, once it is up. */
export function forwardWorksMethods(
	instance: () => Promise<WorksModalsMethods>
): WorksModalsMethods {
	return {
		async stackTracks(ids: string[], title: string): Promise<void> {
			const target = await instance();
			return target.stackTracks(ids, title);
		}
	};
}

/** The version stack panel imports only for a non-null stack id. */
export function requestStackPanel<M>(stableId: string | null, lazy: FirstUseImport<M>): void {
	if (stableId !== null) void lazy.load().catch(() => {});
}
