/**
 * Direct client for the macOS OpenDJ local-agent LaunchAgent.
 *
 * Talks to loopback :18765 (not FastAPI :8585) so "Agent fix servers" still
 * works when the backend is down. See docs/opendj-local-agent.md.
 */

export type LocalAgentAction = 'run-servers' | 'claude-run-servers';

export type LocalAgentHealth = {
	ok: boolean;
	service?: string;
	repo?: string;
	actions?: string[];
	secret_required?: boolean;
};

export type LocalAgentAccept = {
	ok: boolean;
	accepted?: boolean;
	action?: string;
	pid?: number;
	repo?: string;
	error?: string;
	detail?: string;
};

const DEFAULT_BASE = 'http://127.0.0.1:18765';

function _base(): string {
	const fromEnv = String(import.meta.env.VITE_OPENDJ_LOCAL_AGENT_URL ?? '').trim();
	return (fromEnv || DEFAULT_BASE).replace(/\/$/, '');
}

function _secret(): string {
	return String(import.meta.env.VITE_OPENDJ_LOCAL_AGENT_SECRET ?? '').trim();
}

function _headers(jsonBody: boolean): HeadersInit {
	const h: Record<string, string> = { Accept: 'application/json' };
	if (jsonBody) h['Content-Type'] = 'application/json';
	const secret = _secret();
	if (secret) h['X-OpenDJ-Secret'] = secret;
	return h;
}

/** Soft probe: returns null when agent is not installed / unreachable. */
export async function probeLocalAgent(timeoutMs = 800): Promise<LocalAgentHealth | null> {
	const ctrl = new AbortController();
	const t = window.setTimeout(() => ctrl.abort(), timeoutMs);
	try {
		const r = await fetch(`${_base()}/health`, {
			method: 'GET',
			headers: _headers(false),
			signal: ctrl.signal,
			cache: 'no-store'
		});
		if (!r.ok) return null;
		return (await r.json()) as LocalAgentHealth;
	} catch {
		return null;
	} finally {
		window.clearTimeout(t);
	}
}

/**
 * Ask the local agent to run an action. Throws a short Error message suitable
 * for toasts (never throws network stack traces).
 */
export async function requestLocalAgent(
	action: LocalAgentAction,
	timeoutMs = 2500
): Promise<LocalAgentAccept> {
	const ctrl = new AbortController();
	const t = window.setTimeout(() => ctrl.abort(), timeoutMs);
	try {
		const r = await fetch(`${_base()}/`, {
			method: 'POST',
			headers: _headers(true),
			body: JSON.stringify({ action }),
			signal: ctrl.signal,
			cache: 'no-store'
		});
		let body: LocalAgentAccept = { ok: false };
		try {
			body = (await r.json()) as LocalAgentAccept;
		} catch {
			/* non-JSON */
		}
		if (r.status === 401) {
			throw new Error(
				'Local agent requires secret (set VITE_OPENDJ_LOCAL_AGENT_SECRET to match ~/.opendj/local-agent.env)'
			);
		}
		if (!r.ok || body.ok === false) {
			throw new Error(body.error || body.detail || `Local agent HTTP ${r.status}`);
		}
		return body;
	} catch (err) {
		if (err instanceof Error && err.name === 'AbortError') {
			throw new Error('Local agent timed out (is launchd agent installed?)');
		}
		if (err instanceof TypeError) {
			// fetch network failure (connection refused, CORS, offline)
			throw new Error(
				'Local agent not reachable on :18765 -- run `just install-local-agent` (see docs/opendj-local-agent.md)'
			);
		}
		throw err instanceof Error ? err : new Error(String(err));
	} finally {
		window.clearTimeout(t);
	}
}
