/**
 * One dynamic import, attempted at most once (per instance; a loader keeps its instance in
 * <script module>, so once per document). A failed import() stays failed for that document,
 * because the browser keeps it in its module map (see retrySetupOverlay in routes/+layout.svelte),
 * so a failure is cached and returned, never retried. The returned promise never rejects.
 */
export type LazyImportResult<T> = { ok: true; value: T } | { ok: false; error: string };

export interface LazyOnceImport<T> {
	/** Starts the import on the first call. Every call returns the same promise. */
	request(): Promise<LazyImportResult<T>>;
	/** True once request() has been called. */
	readonly requested: boolean;
}

export function lazyImportErrorText(exc: unknown): string {
	return exc instanceof Error ? exc.message : String(exc);
}

export function createLazyOnceImport<T>(
	importer: () => Promise<{ default: T }>,
	onError?: (exc: unknown) => void
): LazyOnceImport<T> {
	let pending: Promise<LazyImportResult<T>> | null = null;
	return {
		request(): Promise<LazyImportResult<T>> {
			if (pending === null) {
				pending = Promise.resolve()
					.then(importer)
					.then(
						(m): LazyImportResult<T> => ({ ok: true, value: m.default }),
						(exc: unknown): LazyImportResult<T> => {
							onError?.(exc);
							return { ok: false, error: lazyImportErrorText(exc) };
						}
					);
			}
			return pending;
		},
		get requested(): boolean {
			return pending !== null;
		}
	};
}
