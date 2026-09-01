/**
 * App-usage heartbeat -- the client half of "is the app open right now".
 *
 * The engine must be able to answer that itself instead of a human being
 * asked, so every page load checks in on a timer and whenever the page is
 * shown or hidden. Read the answer back with:
 *
 *     GET /api/v1/telemetry/clients
 *
 * Fire-and-forget on purpose: no retry queue, no buffering, no offline
 * spool. If the engine is down the fetch rejects and the client is simply
 * reported stale, which is exactly the truth we want on the other side.
 */

import { API_BASE } from '$lib/api/client';
import { bootScheduler, type BootScheduler } from './boot-scheduler';

export type UsageSurface = 'desktop-shell' | 'browser';

/** Three of these fit inside the engine's 45s window, so one dropped
 *  request never makes a live client read as closed. */
export const HEARTBEAT_INTERVAL_MS = 15_000;

/** The webui package version this bundle ships from. Held in step with
 *  package.json by tests/unit/usage-heartbeat.test.mjs, which fails if the
 *  package is bumped and this constant is not. */
export const APP_VERSION = '0.1.0';

const HEARTBEAT_PATH = '/api/v1/telemetry/heartbeat';

/** The one global the desktop shell injects into whatever page it loads. */
export interface ShellScope {
	OPENDJ_ENGINE_ORIGIN?: unknown;
}

/**
 * Which surface this page is running on.
 *
 * The desktop shell (apps/desktop) is a Tauri 2 window that boots its own
 * bundled setup page and then navigates the webview to the engine origin.
 * Two things follow, both verified against the real installed Open DJ.app
 * 0.1.0 attached to a loopback probe engine on Wed 19 Aug 2026:
 *
 * - `window.__TAURI__` is NOT the signal. It is `undefined` here, because
 *   the shell leaves `withGlobalTauri` off.
 * - `globalThis.OPENDJ_ENGINE_ORIGIN` IS the signal. main.rs injects it as
 *   an `initialization_script`, and WKWebView re-runs that script on every
 *   document in the window, so it survives the cross-origin hop onto the
 *   engine and is present on this page.
 *
 * Using the shell's own baked-origin convention also keeps the thin-shell
 * rule intact (apps/desktop/README.md): this reads a plain global, it does
 * not import `@tauri-apps/api` or call a Tauri IPC command.
 */
export function detectSurface(scope: ShellScope): UsageSurface {
	const injectedOrigin = scope.OPENDJ_ENGINE_ORIGIN;
	return typeof injectedOrigin === 'string' && injectedOrigin !== ''
		? 'desktop-shell'
		: 'browser';
}

/**
 * Start checking in. Returns the teardown, which the caller owns.
 *
 * The client id is per page load, not per install: two tabs are two
 * clients, and a reload is a new one. Nothing is persisted, so there is no
 * identifier to leak and no stale row that outlives the engine.
 */
export function startUsageHeartbeat(scheduler: BootScheduler = bootScheduler): () => void {
	if (typeof window === 'undefined' || typeof document === 'undefined') {
		return () => {};
	}
	const clientId = crypto.randomUUID();
	const surface = detectSurface(window as Window & ShellScope);

	const send = (): void => {
		void fetch(`${API_BASE}${HEARTBEAT_PATH}`, {
			method: 'POST',
			keepalive: true,
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({
				client_id: clientId,
				surface,
				page_visible: document.visibilityState === 'visible',
				app_version: APP_VERSION
			})
		}).catch(() => {
			// An engine that is down is a fact for the other side to observe,
			// never an error thrown into the application path.
		});
	};

	// The FIRST check-in is deferred out of the boot burst (PERF-R6): the
	// engine's window is 45s wide and three intervals fit inside it, so a
	// check-in that lands a few seconds later still reads as live -- while
	// the request it is not making at mount is one more connection the deck
	// load gets to keep. The interval and the visibility listener are
	// untouched; only the boot-window send moves.
	scheduler.defer('usage-heartbeat:first', send);
	const timer = setInterval(send, HEARTBEAT_INTERVAL_MS);
	document.addEventListener('visibilitychange', send);

	return () => {
		clearInterval(timer);
		document.removeEventListener('visibilitychange', send);
	};
}
