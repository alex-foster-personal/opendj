/**
 * Desktop-shell route navigation poll (issue #2866, ADR-0048).
 *
 * Only the installed shell (`OPENDJ_ENGINE_ORIGIN`) consumes
 * `GET /api/v1/shell/navigate/pending`; browser tabs must not auto-navigate.
 */
import { goto } from '$app/navigation';
import { API_BASE } from '$lib/api/base';
import { detectSurface } from '$lib/rb/usage-heartbeat';

const PENDING_PATH = '/api/v1/shell/navigate/pending';
const ACK_PATH = '/api/v1/shell/navigate/ack';
const POLL_MS = 200;

function routeMatches(currentPath: string, targetRoute: string): boolean {
	if (currentPath === targetRoute) return true;
	const prefix = targetRoute.endsWith('/') ? targetRoute : `${targetRoute}/`;
	return currentPath.startsWith(prefix);
}

export function installShellNavigationPoll(): () => void {
	if (typeof window === 'undefined') {
		return () => {};
	}
	if (detectSurface(window as Window & { OPENDJ_ENGINE_ORIGIN?: unknown }) !== 'desktop-shell') {
		return () => {};
	}

	let lastHandledId: string | null = null;
	let handling = false;

	const tick = async (): Promise<void> => {
		if (handling) return;
		handling = true;
		try {
			const response = await fetch(`${API_BASE}${PENDING_PATH}`);
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
			}
			const ack = await fetch(`${API_BASE}${ACK_PATH}`, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ id: pending.id })
			});
			if (ack.ok) {
				lastHandledId = pending.id;
			}
		} finally {
			handling = false;
		}
	};

	const intervalId = setInterval(() => {
		void tick();
	}, POLL_MS);
	void tick();
	return () => clearInterval(intervalId);
}
