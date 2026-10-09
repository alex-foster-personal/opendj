/**
 * STRUCT-05: on-demand loader for the section overlay painter (sections.ts).
 *
 * `show_sections` defaults OFF, and the browser preview strip, the main
 * waveform painter (render.ts) and the deck strip all sit in the "/" static
 * import closure, so a static import of sections.ts made every library
 * first paint download the label tables and band painter that most users
 * never switch on (bundle budget `library`, scripts/bundle-budget.mjs). The
 * painter is fetched behind this dynamic import the first time a surface
 * asks for it with the pref on, and the module is cached for every later
 * call.
 *
 * Reactivity: `_loaded` is rune state, so a component effect or derived that
 * calls `sectionsApiFor(true)` before the chunk arrives returns null (no
 * bands) and re-runs once it lands, which is what makes the bands appear
 * after toggling the pref on without a reload.
 */
import type * as SectionsModule from './sections';

export type SectionsApi = typeof SectionsModule;

let _api: SectionsApi | null = null;
let _loaded = $state(false);
let _pending: Promise<SectionsApi> | null = null;

/** Backoff after a failed fetch, so a playing deck that redraws every frame
 * does not re-request a dead chunk (say a stale asset hash after a deploy)
 * 60 times a second. Doubles per consecutive failure, capped. */
const RETRY_BASE_MS = 1_000;
const RETRY_MAX_MS = 60_000;
let _failures = 0;
let _retryAt = 0;
/** One pending retry timer at most. A surface that only repaints on events
 * (a stopped deck strip, the browser preview) may never call again after the
 * window passes, so the retry is scheduled rather than left to the next
 * redraw; its success flips `_loaded`, which re-runs those surfaces. */
let _retryTimer: ReturnType<typeof setTimeout> | null = null;
/** Set once any surface has asked for the painter. A retry only runs for a
 * painter someone wanted; it stays set because `want` false from one surface
 * (a row with no analysis) says nothing about the others. */
let _wanted = false;

function _logLoadFailure(err: unknown): void {
	console.error('section overlay: painter module failed to load', err);
}

function _retryNow(): void {
	_retryTimer = null;
	if (_loaded || _pending !== null || !_wanted) return;
	void loadSectionsApi().catch(_logLoadFailure);
}
/** The last fetch failure, for the UI: Show sections must not look like a
 * track with no sections while its painter cannot load. Rune state. */
let _loadError = $state<string | null>(null);

/** Why the section painter failed to load, or null when it has not failed
 * (or has since loaded). Reactive. */
export function sectionsLoadError(): string | null {
	return _loadError;
}

/** Fetch (once) and cache the section painter module. A failed fetch clears
 * the pending promise so a later call retries, and rejects loudly. */
export function loadSectionsApi(): Promise<SectionsApi> {
	_pending ??= import('./sections').then(
		(mod) => {
			_api = mod;
			_failures = 0;
			_retryAt = 0;
			_loadError = null;
			_loaded = true;
			return mod;
		},
		(err: unknown) => {
			_pending = null;
			const delay = Math.min(RETRY_BASE_MS * 2 ** _failures, RETRY_MAX_MS);
			_retryAt = Date.now() + delay;
			_failures += 1;
			if (_wanted && _retryTimer === null) _retryTimer = setTimeout(_retryNow, delay);
			_loadError = err instanceof Error ? err.message : String(err);
			throw err;
		}
	);
	return _pending;
}

/**
 * The section painter when `want` is true and the module has loaded; null
 * otherwise. With `want` true and the module not yet loaded this starts the
 * fetch and returns null; a caller inside an effect or derived re-runs when
 * it lands. With `want` false nothing is fetched. After a failed fetch it
 * returns null without refetching until the backoff window has passed, and a
 * timer retries once the window ends even if no surface redraws.
 */
export function sectionsApiFor(want: boolean): SectionsApi | null {
	if (!want) return null;
	_wanted = true;
	if (!_loaded) {
		// In flight: the call that started the fetch already holds the one
		// logger, so later frames add no handler and no promise.
		if (_pending !== null) return null;
		if (Date.now() < _retryAt) {
			if (_retryTimer === null) _retryTimer = setTimeout(_retryNow, _retryAt - Date.now());
			return null;
		}
		void loadSectionsApi().catch(_logLoadFailure);
		return null;
	}
	return _api;
}
