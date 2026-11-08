/**
 * One background library refresh pass, in order: side reads, then the fresh
 * track count and loaded panes, then the stranded-boot-pane retry.
 *
 * The side reads (ingest coverage, reconcile summary, playlist tree) are
 * SETTLED, not Promise.all'd: one of them rejecting must not skip the boot-pane
 * retry, which is the only thing that ever repairs a pane `_init()` left blank
 * on a stale zero-track snapshot (issue #1764, PR #1656 thread r3978915642).
 */
export interface LibraryRefreshPassSteps {
	/** Run together, first. A rejection is reported, never propagated. */
	readonly sideLoads: ReadonlyArray<() => Promise<void>>;
	/** Fresh count + loaded panes. Resolves false when the page is going away,
	 *  which ends the pass without the retry (nothing is left to repair). */
	readonly refreshCountAndPanes: () => Promise<boolean>;
	/** Decides (shouldRetryBootPane) and, if so, restores the boot pane. */
	readonly retryBootPane: () => Promise<void>;
	readonly onSideLoadError: (exc: unknown) => void;
}

export async function runLibraryRefreshPass(steps: LibraryRefreshPassSteps): Promise<void> {
	// async wrapper: a load that throws synchronously still becomes a rejection.
	const settled = await Promise.allSettled(steps.sideLoads.map(async (load) => load()));
	for (const result of settled) {
		if (result.status === 'rejected') steps.onSideLoadError(result.reason);
	}
	if (!(await steps.refreshCountAndPanes())) return;
	await steps.retryBootPane();
}
