/**
 * Desktop-shell route navigation poll (issue #2866, ADR-0048; hardened for
 * issue #2879).
 *
 * Only the installed shell (`OPENDJ_ENGINE_ORIGIN`) consumes
 * `GET /api/v1/shell/navigate/pending`; browser tabs must not auto-navigate.
 */
import { goto } from '$app/navigation';
import { API_BASE } from '$lib/api/base';
import { detectSurface } from '$lib/rb/usage-heartbeat';
import { pushToast } from '$lib/stores.svelte';

const PENDING_PATH = '/api/v1/shell/navigate/pending';
const ACK_PATH = '/api/v1/shell/navigate/ack';
// Measured (issue #2879): 5_000 landed exactly 12 lines in a 60s idle window,
// the acceptance ceiling with no margin. 6_000 measures 10-11, comfortably
// under it, while still satisfying the ">=5s" acceptance floor.
export const POLL_MS = 6_000;

function routeMatches(currentPath: string, targetRoute: string): boolean {
	if (currentPath === targetRoute) return true;
	const prefix = targetRoute.endsWith('/') ? targetRoute : `${targetRoute}/`;
	return currentPath.startsWith(prefix);
}

export function installShellNavigationPoll(): () => void {
	if (typeof window === 'undefined' || typeof document === 'undefined') {
		return () => {};
	}
	if (detectSurface(window as Window & { OPENDJ_ENGINE_ORIGIN?: unknown }) !== 'desktop-shell') {
		return () => {};
	}

	let lastHandledId: string | null = null;
	let handling = false;
	// Set the moment a tick fails to reach the engine, cleared the moment one
	// succeeds again -- this is what turns "one log line per tick during an
	// outage" into "one log line per outage".
	let failingSince: number | null = null;
	let intervalId: ReturnType<typeof setInterval> | null = null;

	const tick = async (): Promise<void> => {
		if (handling) return;
		handling = true;
		try {
			const response = await fetch(`${API_BASE}${PENDING_PATH}`);
			failingSince = null;
			if (!response.ok) return;
			const body = (await response.json()) as {
				pending?: { id?: string; route?: string } | null;
			};
			const pending = body.pending;
			if (
				pending == null ||
				typeof pending.id !== 'string' ||
				typeof pending.route !== 'string' ||
				pending.id === lastHandledId
			) {
				return;
			}
			const route = pending.route;
			if (!routeMatches(window.location.pathname, route)) {
				await goto(route);
				pushToast(
					`An agent moved this window to ${route}.`,
					'info',
					undefined,
					undefined,
					{},
					'shell-navigate-agent-move'
				);
			}
			const ack = await fetch(`${API_BASE}${ACK_PATH}`, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ id: pending.id })
			});
			if (ack.ok) {
				lastHandledId = pending.id;
			}
		} catch (error) {
			// A down engine is a fact for this poller to record once, never an
			// unhandled rejection per tick (issue #2879) and never a silent
			// swallow: the first tick of an outage logs it, every tick after
			// that until recovery is a duplicate of a fact already recorded.
			if (failingSince == null) {
				failingSince = Date.now();
				console.warn(
					`shell navigation poll failing since ${new Date(failingSince).toISOString()}`,
					error
				);
			}
		} finally {
			handling = false;
		}
	};

	const stopInterval = (): void => {
		if (intervalId == null) return;
		clearInterval(intervalId);
		intervalId = null;
	};

	const startInterval = (): void => {
		if (intervalId != null) return;
		intervalId = setInterval(() => void tick(), POLL_MS);
	};

	// A hidden window (minimized, other-tab, other-space) gets none of this
	// traffic; `visibilitychange` restarts it (and takes one tick right away)
	// the moment it is shown again, per issue #2879.
	const onVisibilityChange = (): void => {
		if (document.hidden) {
			stopInterval();
			return;
		}
		startInterval();
		void tick();
	};

	document.addEventListener('visibilitychange', onVisibilityChange);
	if (!document.hidden) {
		startInterval();
		void tick();
	}

	return () => {
		stopInterval();
		document.removeEventListener('visibilitychange', onVisibilityChange);
	};
}
