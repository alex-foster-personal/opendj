/** Google sign-in state for the user bauble.
 *
 * Thin client over the daemon's /api/v1/auth/* endpoints. The session lives
 * in an httpOnly cookie the daemon sets, so there is no token to hold here
 * and nothing to persist in localStorage -- `refreshUser` asks the daemon
 * who we are and that is the only source of truth.
 *
 * `credentials: 'same-origin'` is explicit rather than relying on the fetch
 * default: in dev the SPA and the API share an origin only because Vite
 * proxies /api, and being explicit keeps that working if the base ever
 * moves.
 */
import { API_BASE } from './api';

export interface AuthUser {
	google_sub: string;
	email: string;
	name: string | null;
	avatar_url: string | null;
	created_at: string;
}

export interface LoginStart {
	authorization_url: string;
	state: string;
	redirect_uri: string;
}

/** `user` is null when signed out. `error` holds the last failure, if any. */
export const auth = $state<{
	user: AuthUser | null;
	loading: boolean;
	error: string | null;
}>({ user: null, loading: true, error: null });

async function authFetch(path: string, init: RequestInit = {}): Promise<Response> {
	return fetch(`${API_BASE}${path}`, {
		...init,
		credentials: 'same-origin',
		headers: { Accept: 'application/json', 'Content-Type': 'application/json', ...(init.headers || {}) }
	});
}

/** Read the daemon's error envelope, which is always {detail: {code, message}}. */
async function errorMessage(response: Response, fallback: string): Promise<string> {
	try {
		const body = await response.json();
		const detail = body?.detail;
		if (typeof detail === 'string') return detail;
		if (detail?.message) return String(detail.message);
	} catch {
		// Body was not JSON. Fall through to the caller's wording rather
		// than surfacing a parse error the user cannot act on.
	}
	return fallback;
}

/** Ask the daemon who is signed in. A 401 is the signed-out answer, not a failure. */
export async function refreshUser(): Promise<void> {
	auth.loading = true;
	try {
		const response = await authFetch('/api/v1/auth/me');
		if (response.status === 401) {
			auth.user = null;
			auth.error = null;
			return;
		}
		if (!response.ok) {
			auth.user = null;
			auth.error = await errorMessage(response, `sign-in check failed (${response.status})`);
			return;
		}
		auth.user = (await response.json()) as AuthUser;
		auth.error = null;
	} catch (exc) {
		auth.user = null;
		auth.error = exc instanceof Error ? exc.message : 'daemon unreachable';
	} finally {
		auth.loading = false;
	}
}

/**
 * Start Google sign-in and hand back the consent URL.
 *
 * Navigation is the caller's job so the click that opens Google's consent
 * screen stays inside the user gesture that triggered it. Throws on failure
 * -- a 503 here means the daemon has no OAuth client configured, and its
 * message is the provisioning runbook.
 */
export async function startLogin(): Promise<LoginStart> {
	const response = await authFetch('/api/v1/auth/login', {
		method: 'POST',
		body: JSON.stringify({ origin: window.location.origin })
	});
	if (!response.ok) {
		throw new Error(await errorMessage(response, `could not start sign-in (${response.status})`));
	}
	return (await response.json()) as LoginStart;
}

/** Drop the session server-side, then clear it locally. */
export async function logout(): Promise<void> {
	const response = await authFetch('/api/v1/auth/logout', { method: 'POST' });
	if (!response.ok) {
		throw new Error(await errorMessage(response, `sign-out failed (${response.status})`));
	}
	auth.user = null;
	auth.error = null;
}
