/**
 * Open DJ shell bootstrap.
 *
 * Requirements (mini-PRD):
 *
 * - ✔︎ ✅ Resolve the engine origin from exactly one of three explicit
 *   sources, never silently. -> `resolveEngineOrigin`
 * - ✔︎ ✅ Probe the engine and report reachable / unreachable with the
 *   reason attached. -> `probeEngine`
 * - ✔︎ ✅ Reachable: leave this page for the engine. Unreachable: render a
 *   setup screen naming the address, the failure and the fix.
 *   -> `startBootstrap`
 * - ✔︎ ✅ Never render a third state: no blank window, no endless spinner,
 *   no fabricated library data.
 *
 * Acceptance tests:
 *
 * - [if] nothing listens on the engine port [then] the setup screen shows
 *   the exact address tried and a non-empty failure reason, [else ⛔️].
 * - [if] the engine answers [then] the page navigates to the engine
 *   origin and stops polling, [else ⛔️].
 * - [if] `?engine=` is absent and no origin was injected [then]
 *   `resolveEngineOrigin` returns DEFAULT_ENGINE_ORIGIN, [else ⛔️].
 * - [if] an origin is supplied but is not a loopback http(s) URL [then]
 *   resolution throws rather than probing it, [else ⛔️].
 */

/**
 * What a shipped build talks to. This is the ONLY place the shell decides
 * a port, and the value is rendered on screen whenever it is used, so it
 * is a stated default rather than a hidden one.
 */
export const DEFAULT_ENGINE_ORIGIN = 'http://127.0.0.1:8685';

/** Milliseconds between unattended reconnect attempts. */
export const RETRY_INTERVAL_MS = 3000;

const HEALTH_PATH = '/api/v1/health';
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1']);

/**
 * Resolve the engine origin, most specific source first.
 *
 * 1. `?engine=` on this page's URL. The test seam, mirroring the repo's
 *    ENGINE_CMD seam: it lets the real screen be driven against a port
 *    that is guaranteed dead without rebuilding the app.
 * 2. `globalThis.OPENDJ_ENGINE_ORIGIN`, injected by the Rust shell from
 *    the `OPENDJ_ENGINE_ORIGIN` environment variable.
 * 3. DEFAULT_ENGINE_ORIGIN.
 *
 * Anything present but unusable throws. The shell must not quietly probe
 * a different address than the operator asked for.
 */
export function resolveEngineOrigin(search = '', injected = undefined) {
	const fromQuery = new URLSearchParams(search).get('engine');
	if (fromQuery !== null) {
		return assertLoopbackOrigin(fromQuery, '?engine=');
	} else if (typeof injected === 'string') {
		return assertLoopbackOrigin(injected, 'OPENDJ_ENGINE_ORIGIN');
	}
	return DEFAULT_ENGINE_ORIGIN;
}

function assertLoopbackOrigin(raw, source) {
	const trimmed = raw.trim();
	if (trimmed === '') {
		throw new Error(`${source} is set but empty; give a full origin or unset it`);
	}
	let parsed;
	try {
		parsed = new URL(trimmed);
	} catch {
		throw new Error(`${source}=${trimmed} is not a URL`);
	}
	if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
		throw new Error(`${source}=${trimmed} must be http or https`);
	}
	if (!LOOPBACK_HOSTS.has(parsed.hostname)) {
		throw new Error(
			`${source}=${trimmed} is not loopback; the engine is a local process`
		);
	}
	return parsed.origin;
}

export function healthUrl(origin) {
	return `${origin}${HEALTH_PATH}`;
}

/**
 * Ask whether anything is answering HTTP at the engine's health address.
 *
 * `no-cors` is deliberate and its limits are stated on screen. The engine
 * allows CORS only for the dev server origins, so a normal cross origin
 * read from `tauri://localhost` would be blocked by the browser before it
 * could distinguish "engine refused" from "engine absent". `no-cors`
 * gives one honest bit: the connection was accepted, or it was not.
 */
export async function probeEngine(origin, fetchImpl = globalThis.fetch) {
	const url = healthUrl(origin);
	try {
		await fetchImpl(url, { mode: 'no-cors', cache: 'no-store' });
		return { reachable: true, url, detail: 'connection accepted' };
	} catch (err) {
		const reason = err instanceof Error ? err.message : String(err);
		return {
			reachable: false,
			url,
			detail: `no response (${reason || 'connection refused'})`
		};
	}
}

export function engineCommand(origin) {
	const { hostname, port } = new URL(origin);
	const resolvedPort = port === '' ? '80' : port;
	return [
		'uv run python -m apps.engine_core serve \\',
		'  --data-dir /absolute/path/to/your/data \\',
		`  --host ${hostname} --port ${resolvedPort}`
	].join('\n');
}

// ----- DOM wiring ---------------------------------------------------------

function byId(id) {
	const el = document.getElementById(id);
	if (el === null) {
		throw new Error(`setup page is missing #${id}; index.html and setup.js disagree`);
	}
	return el;
}

function showUnreachable(result, attempts) {
	byId('root').dataset.state = 'unreachable';
	byId('checking-view').hidden = true;
	byId('unreachable-view').hidden = false;
	byId('probe-url').textContent = result.url;
	byId('probe-url-echo').textContent = result.url;
	byId('probe-detail').textContent = result.detail;
	byId('attempts').textContent = String(attempts);
	if (typeof globalThis.__OPENDJ_enqueueShellClientError === 'function') {
		globalThis.__OPENDJ_enqueueShellClientError(
			'webview-navigation',
			`engine unreachable: ${result.detail}`,
			{ source: 'shell-bootstrap', url: result.url }
		);
	}
}

/**
 * Drive the page. Exported so a harness can run it with an injected
 * fetch and navigate function instead of the real ones.
 */
export function startBootstrap({
	origin,
	fetchImpl = globalThis.fetch,
	navigate = (target) => globalThis.location.replace(target),
	intervalMs = RETRY_INTERVAL_MS
} = {}) {
	let attempts = 0;
	let stopped = false;
	let timer = null;

	byId('checking-origin').textContent = origin;
	byId('engine-command').textContent = engineCommand(origin);

	const countdown = byId('countdown');

	async function attempt() {
		if (stopped) {
			return;
		}
		attempts += 1;
		const result = await probeEngine(origin, fetchImpl);
		if (stopped) {
			return;
		}
		if (result.reachable) {
			stopped = true;
			countdown.textContent = 'Engine found. Opening Open DJ.';
			navigate(`${origin}/`);
			return;
		}
		showUnreachable(result, attempts);
		countdown.textContent = `Retrying every ${Math.round(intervalMs / 1000)}s.`;
		timer = globalThis.setTimeout(attempt, intervalMs);
	}

	byId('retry').addEventListener('click', () => {
		if (timer !== null) {
			globalThis.clearTimeout(timer);
			timer = null;
		}
		countdown.textContent = 'Checking.';
		void attempt();
	});

	void attempt();

	return {
		stop() {
			stopped = true;
			if (timer !== null) {
				globalThis.clearTimeout(timer);
				timer = null;
			}
		}
	};
}

// Auto-start only in a real page, never when imported by a test runner.
if (typeof document !== 'undefined' && document.getElementById('root') !== null) {
	startBootstrap({
		origin: resolveEngineOrigin(
			globalThis.location?.search ?? '',
			globalThis.OPENDJ_ENGINE_ORIGIN
		)
	});
}
