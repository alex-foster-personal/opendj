/**
 * The right-click registry, loaded off the first-paint path (W28).
 *
 * TrackContextMenu and TreeContextMenu sit in the /performance boot closure
 * (TrackTable and the playlist tree import them statically), so a static
 * import of `./registry` from them would put every menu's declarative data on
 * first paint, measured at +2.1 KB gzip against a budget with 2 KB of room.
 * A menu is never shown before the first right-click, so those hosts load it
 * here instead: `preloadMenuRegistry()` fetches it once the page is idle, and
 * `loadMenuRegistry()` on open covers a right-click that beats the preload,
 * and has the host raise an error toast if the chunk cannot load.
 * Quick Draw is already lazy (QuickDrawMenuLoader) and keeps its static import.
 */
type Registry = typeof import('./registry');

let loaded = $state<Registry | null>(null);
let pending: Promise<void> | null = null;
/** Tells the user a right-click's menu could not load. The host supplies it,
 * so this module stays free of the toast store. */
export type MenuLoadFailureReporter = (cause: unknown) => void;

/** Set while a right-click is waiting on the chunk, so a failure reaches the user. */
let waiting: MenuLoadFailureReporter | null = null;

/** The registry when it has loaded, else null (reactive). */
export function menuRegistry(): Registry | null {
	return loaded;
}

function fetchRegistry(): void {
	pending ??= import('./registry').then(
		(m) => {
			loaded = m;
			waiting = null;
		},
		(e: unknown) => {
			pending = null;
			const report = waiting;
			waiting = null;
			if (report === null) {
				// Idle preload only: nobody is looking at a menu yet, and the
				// next right-click tries again and reports its own failure.
				console.error('[context-menu] registry chunk failed to preload', e);
				return;
			}
			// The right-click already suppressed the browser menu, so a silent
			// failure would leave the user with no menu and no reason.
			report(e);
		}
	);
}

/** Load the registry for a right-click that is opening a menu now. If the
 * chunk fails, `onFailure` tells the user. */
export function loadMenuRegistry(onFailure: MenuLoadFailureReporter): void {
	waiting = onFailure;
	fetchRegistry();
}

/** Fetch the registry when the browser is idle, so a right-click finds it warm. */
export function preloadMenuRegistry(): void {
	if (loaded !== null || pending !== null) return;
	if (typeof requestIdleCallback === 'function') requestIdleCallback(fetchRegistry, { timeout: 2000 });
	else setTimeout(fetchRegistry, 0);
}
