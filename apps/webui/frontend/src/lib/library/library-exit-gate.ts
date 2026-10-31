/**
 * LIBWS-06: a client-side way out of Library to a mode route waits out a Gig
 * teardown still in flight, as the mode picker and Load to deck do (Sol P1,
 * PR #6189). A Gig remount that lands while the old release is disposing
 * strands the old decks or has the new engine cleared.
 *
 * SvelteKit's `beforeNavigate` can cancel only synchronously, so this cancels
 * the navigation and replays it once the release settles: Back/Forward by the
 * same history step (SvelteKit has already undone the step on cancel), a goto
 * or link by its URL. Other kinds (a form, a page leave) are not held.
 */

interface LibraryExitNavigation {
	type: string;
	delta?: number | undefined;
	to: { url: URL } | null;
	cancel(): void;
}

interface LibraryExitGateDeps {
	pending(): boolean;
	settled(): Promise<void>;
	go(delta: number): void;
	goto(url: URL): Promise<void>;
}

/** True when the navigation was cancelled and will be replayed. */
export function holdLibraryExitForGigRelease(
	navigation: LibraryExitNavigation,
	deps: LibraryExitGateDeps
): boolean {
	const to = navigation.to;
	if (to === null || !deps.pending()) return false;
	let replay: (() => void) | null = null;
	if (navigation.type === 'popstate' && typeof navigation.delta === 'number') {
		const delta = navigation.delta;
		replay = () => deps.go(delta);
	} else if (navigation.type === 'goto' || navigation.type === 'link') {
		replay = () => void deps.goto(to.url);
	}
	if (replay === null) return false;
	navigation.cancel();
	void deps.settled().then(replay);
	return true;
}
