/**
 * When a `browser_select_playlist` order answers (LIBM, agent parity).
 *
 * The selection has LANDED once its pane shows its first rows or its error:
 * that is what a person sees when they click. The rest of a large fill keeps
 * going after the answer (All Tracks loads the whole library index in one
 * request, which took more than 20 s on a loaded preview, so the order timed
 * out after the playlist had visibly switched).
 *
 * `onLanded` subscribes to the pane leaving its loading state and returns the
 * unsubscribe. The full `load` still settles the answer if it finishes first
 * (an already-held index, a small playlist).
 */
export async function untilSelectionLands(
	load: Promise<void>,
	onLanded: (landed: () => void) => () => void
): Promise<void> {
	let stop: (() => void) | null = null;
	let done = false;
	try {
		await Promise.race([
			load,
			new Promise<void>((resolve) => {
				stop = onLanded(() => {
					done = true;
					resolve();
				});
				if (done) stop();
			})
		]);
	} finally {
		(stop as (() => void) | null)?.();
	}
}
