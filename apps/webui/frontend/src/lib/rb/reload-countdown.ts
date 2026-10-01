/**
 * REFRESH-01 (issue #891): announce a full page reload before taking it.
 *
 * A reload that lands mid-thought costs whatever was on screen - a half-read
 * diff, a state worth screenshotting, a half-typed comment. So while the maintainer is
 * actually looking at the tab, a reload gets a short 3-2-1 on-top countdown
 * first. It was 10 s until Thu 1 Oct 2026, when the maintainer asked for 3-2-1: the
 * preview loop reloads on every integrate, and three seconds is still enough
 * to see it coming.
 * While the tab is hidden it does not: a countdown nobody can see is latency
 * with a UI on top, so a background reload happens at once.
 *
 * The scheduler is pure and takes its effects, so the unit suite drives it
 * with a fake clock instead of waiting real seconds.
 *
 * ## What this can and cannot hold back
 *
 * The app's own reloads: all of them - `scheduleReload()` is the only way in.
 * There are currently NO other `location.reload()` call sites in src, so any
 * future one must route through here.
 *
 * Vite dev full reloads: held too, but not from here. In the vite pinned
 * here (5.4.21), `client.mjs`'s `case "full-reload"` branch calls
 * `notifyListeners("vite:beforeFullReload", ...)` and then `pageReload()` on
 * the next line unconditionally, ~50ms later; `notifyListeners` is `async`
 * and runs under `Promise.allSettled` with its result neither awaited nor
 * caught, so a throwing listener becomes an unhandled rejection and
 * `pageReload()` runs anyway. The advice that throwing cancels the reload is
 * true of older vite, not this one, and `pageReload` is a module-local
 * closure with no seam; `location.reload` cannot be patched either (it is
 * LegacyUnforgeable). Nothing on this side of the socket can hold it back.
 *
 * So the hold happens server-side instead: `../vite-hold-full-reload.ts`
 * wraps `server.hot.send` and rewrites an outgoing `full-reload` payload into
 * a `mdt:full-reload` custom event before it ever reaches the client - the
 * vite client never sees a `full-reload` message at all, so its unconditional
 * `pageReload()` never runs.
 *
 * That custom event is received by a small inline script the same plugin
 * injects into every page's `<head>` (not by this module): this module's own
 * `installReloadCountdown()` only runs from the root layout's `onMount`,
 * which is too late to catch a reload caused by the app failing to compile
 * in the first place (r3918992964). The head script hands the event to this
 * module's `window.__mdtScheduleReload` once that has been installed, or
 * reloads immediately - matching plain Vite - if it never was.
 */

export const RELOAD_COUNTDOWN_S = 3;

export interface ReloadEffects {
	/** False when the tab is hidden or the app is in the background. */
	isVisible: () => boolean;
	reload: () => void;
	/** Call `fn` once a second; returns the canceller. */
	everySecond: (fn: () => void) => () => void;
	/** Call `fn` when the tab becomes hidden while a countdown is running;
	 * returns the unregister. A countdown nobody can see is pure latency, so
	 * this completes it at once rather than waiting out the remaining ticks. */
	onHidden: (fn: () => void) => () => void;
	render: (secondsLeft: number, reason: string) => void;
	clear: () => void;
}

export interface ReloadScheduler {
	schedule: (reason: string, seconds?: number) => void;
	/** Cancels a running countdown without reloading. No-op if none is
	 * running - the caller (a component teardown) does not know or care. */
	cancel: () => void;
}

export function createReloadScheduler(fx: ReloadEffects): ReloadScheduler {
	let counting = false;
	let stopTimer: (() => void) | null = null;
	let stopHidden: (() => void) | null = null;

	function stop(): void {
		stopTimer?.();
		stopTimer = null;
		stopHidden?.();
		stopHidden = null;
		fx.clear();
		counting = false;
	}

	return {
		schedule(reason: string, seconds: number = RELOAD_COUNTDOWN_S): void {
			// A reload already being counted down keeps ITS deadline: restarting
			// would move the number under a reader mid-glance, and stacking would
			// reload twice.
			if (counting) return;
			if (!fx.isVisible() || !(seconds > 0)) {
				fx.reload();
				return;
			}
			counting = true;
			let left = seconds;
			fx.render(left, reason);
			stopTimer = fx.everySecond(() => {
				left -= 1;
				fx.render(left, reason);
				if (left > 0) return;
				stop();
				fx.reload();
			});
			stopHidden = fx.onHidden(() => {
				stop();
				fx.reload();
			});
		},
		cancel(): void {
			if (!counting) return;
			stop();
		}
	};
}

// ---------------------------------------------------------------- browser

const OVERLAY_ID = 'mdt-reload-countdown';
const OVERLAY_CSS =
	'position:fixed;inset:0;z-index:2147483647;display:flex;flex-direction:column;' +
	'align-items:center;justify-content:center;gap:8px;background:rgba(6,8,11,.82);' +
	'color:#f0f2f5;font:600 14px/1.2 system-ui,sans-serif;text-align:center;' +
	'pointer-events:none';

function _overlay(): HTMLElement {
	let el = document.getElementById(OVERLAY_ID);
	if (el === null) {
		el = document.createElement('div');
		el.id = OVERLAY_ID;
		el.style.cssText = OVERLAY_CSS;
		el.innerHTML = '<div style="font-size:22vmin;font-variant-numeric:tabular-nums"></div><div></div>';
		document.body.appendChild(el);
	}
	return el;
}

/**
 * Install the page's reload announcer. Returns the teardown.
 *
 * Exposes `window.__mdtScheduleReload(reason, seconds?)` so an agent can
 * announce a reload exactly the way the app does - the agent-native parity
 * rule applies to a countdown as much as to a button.
 */
export function installReloadCountdown(): () => void {
	const scheduler = createReloadScheduler({
		isVisible: () => document.visibilityState === 'visible',
		reload: () => location.reload(),
		everySecond: (fn) => {
			const t = setInterval(fn, 1000);
			return () => clearInterval(t);
		},
		onHidden: (fn) => {
			const handler = () => {
				if (document.visibilityState === 'hidden') fn();
			};
			document.addEventListener('visibilitychange', handler);
			return () => document.removeEventListener('visibilitychange', handler);
		},
		render: (secondsLeft, reason) => {
			const el = _overlay();
			el.children[0].textContent = String(Math.max(0, secondsLeft));
			el.children[1].textContent = `reloading: ${reason}`;
		},
		clear: () => document.getElementById(OVERLAY_ID)?.remove()
	});

	// Intersection cast, matching installPerfEventLogGlobal. A double cast
	// through the unknown type would erase Window's own shape on the way past,
	// and the quality gate counts those for exactly that reason - by scanning
	// source text, so naming the pattern here would score as one too.
	const w = window as Window & {
		__mdtScheduleReload?: (reason: string, seconds?: number) => void;
	};
	w.__mdtScheduleReload = (reason, seconds) => scheduler.schedule(reason, seconds);

	// Vite dev full reloads reach this instrument through
	// `window.__mdtScheduleReload` above, not a listener registered here.
	// ../vite-hold-full-reload.ts's injected head script is what actually
	// receives the `mdt:full-reload` custom event (see its docstring for
	// why: this function only runs from onMount, after the whole app has
	// already compiled, which is too late to catch a reload caused by the
	// app failing to compile in the first place).

	return () => {
		// Without this, a countdown still running at teardown leaves its
		// setInterval ticking after the component (and the overlay this
		// clear() also removes below) is gone.
		scheduler.cancel();
		delete w.__mdtScheduleReload;
		document.getElementById(OVERLAY_ID)?.remove();
	};
}
