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
 * - ✔︎ ✅ Every button reacts to being pressed and reports failure in
 *   words, including Relaunch on the engine-fatal screen.
 *   -> `relaunchEngine`
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
 * - [if] Relaunch is pressed with no supervisor health port [then] the
 *   screen says it cannot relaunch and why, [else ⛔️].
 * - [if] the relaunch request is refused [then] the screen names the
 *   address and the refusal, [else ⛔️].
 * - [if] the request is accepted but nothing answers at the engine
 *   address before the timeout [then] the screen reports that the engine
 *   did not come back, [else ⛔️].
 * - [if] the engine answers after the request [then] the page leaves for
 *   the engine origin, [else ⛔️].
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

function showFatal(supervisor) {
	byId('root').dataset.state = 'fatal';
	byId('checking-view').hidden = true;
	byId('unreachable-view').hidden = true;
	byId('fatal-view').hidden = false;
	const exitCode = supervisor?.exit_code ?? '(unknown)';
	const pid = supervisor?.lock_pid ?? '(unknown)';
	const port = supervisor?.lock_port ?? '(unknown)';
	byId('fatal-exit').textContent = String(exitCode);
	byId('fatal-pid').textContent = String(pid);
	byId('fatal-port').textContent = String(port);
}

/**
 * Ask the shell to restart the engine, and SAY WHAT HAPPENED.
 *
 * House rule (the maintainer, Tue 15 Sep 2026): every button reacts to being
 * pressed, and a button whose action fails says so. The first version of
 * this function returned silently when no health port was known and
 * otherwise fired an opaque `no-cors` POST it never looked at, so on the
 * one screen a user reaches only when something is already broken, the
 * only control on it did nothing observable either way.
 *
 * Acceptance is the PRESENCE of a restarted engine, never the absence of
 * a thrown error: an accepted POST proves the supervisor's health server
 * took the request, not that the engine came back. So this polls the
 * engine's own address afterwards and reports success only once
 * something answers there.
 *
 * `no-cors` is kept for both calls for the reason stated on screen: the
 * page is `tauri://localhost` and neither loopback server sends CORS
 * headers, so a normal-mode read would be blocked by the browser and a
 * delivered request would be indistinguishable from a refused one. The
 * opaque form still separates "connection accepted" from "refused",
 * which is the bit that matters here.
 */
export const RELAUNCH_VERIFY_TIMEOUT_MS = 20000;
export const RELAUNCH_POLL_INTERVAL_MS = 500;

export function relaunchUrl(healthPort) {
	return `http://127.0.0.1:${healthPort}/api/v1/relaunch`;
}

export async function relaunchEngine({
	healthPort,
	engineOrigin,
	setStatus,
	fetchImpl = globalThis.fetch,
	probe = probeEngine,
	sleep = defaultSleep,
	now = () => Date.now(),
	timeoutMs = RELAUNCH_VERIFY_TIMEOUT_MS,
	pollIntervalMs = RELAUNCH_POLL_INTERVAL_MS
} = {}) {
	setStatus('working', 'Asking Open DJ to start the engine...');

	if (!healthPort) {
		setStatus(
			'bad',
			'Cannot relaunch: this window was never told which port the app ' +
				'supervisor listens on, so there is nothing to ask. Quit Open DJ ' +
				'and open it again from Finder.'
		);
		return { ok: false, reason: 'no-health-port' };
	}

	const url = relaunchUrl(healthPort);
	try {
		await fetchImpl(url, { method: 'POST', mode: 'no-cors', cache: 'no-store' });
	} catch (err) {
		const detail = err instanceof Error ? err.message : String(err);
		setStatus(
			'bad',
			`Relaunch request was refused at ${url} (${detail || 'connection refused'}). ` +
				'The app supervisor is not answering. Quit Open DJ and open it again from Finder.'
		);
		return { ok: false, reason: 'request-refused', detail };
	}

	setStatus('working', 'Relaunch accepted. Waiting for the engine to answer...');

	const deadline = now() + timeoutMs;
	let lastDetail = 'no response';
	for (;;) {
		const result = await probe(engineOrigin, fetchImpl);
		if (result.reachable) {
			setStatus('good', `Engine is answering at ${engineOrigin}. Opening it...`);
			return { ok: true, url: result.url };
		}
		lastDetail = result.detail;
		if (now() >= deadline) {
			break;
		}
		await sleep(pollIntervalMs);
	}

	setStatus(
		'bad',
		`The engine did not answer at ${engineOrigin} within ` +
			`${Math.round(timeoutMs / 1000)}s of the relaunch (${lastDetail}). ` +
			'It failed to start again. Quit Open DJ and open it again from Finder.'
	);
	return { ok: false, reason: 'engine-absent', detail: lastDetail };
}

function defaultSleep(ms) {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

function showUnreachable(result, attempts) {
	byId('root').dataset.state = 'unreachable';
	byId('checking-view').hidden = true;
	byId('fatal-view').hidden = true;
	byId('unreachable-view').hidden = false;
	byId('probe-url').textContent = result.url;
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

	byId('engine-command').textContent = engineCommand(origin);

	const countdown = byId('countdown');
	const checkingAttempts = byId('checking-attempts');
	const checkingAttemptCount = byId('checking-attempt-count');

	async function attempt() {
		if (stopped) {
			return;
		}
		attempts += 1;
		if (checkingAttempts !== null) {
			checkingAttempts.hidden = false;
			checkingAttemptCount.textContent = String(attempts);
		}
		const result = await probeEngine(origin, fetchImpl);
		if (stopped) {
			return;
		}
		if (result.reachable) {
			stopped = true;
			countdown.textContent = 'Engine found. Opening Open DJ.';
			navigate(`${origin}/performance`);
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

function parseFatalQuery(search) {
	const params = new URLSearchParams(search);
	if (params.get('fatal') !== '1') {
		return null;
	}
	return {
		exit_code: Number(params.get('exit') ?? -1),
		lock_pid: Number(params.get('pid') ?? 0),
		lock_port: Number(params.get('port') ?? 0),
		health_port: globalThis.__OPENDJ_ENGINE_SUPERVISOR__?.health_port ?? null
	};
}

/**
 * Where the engine should be answering once it restarts. The dead
 * engine's own port is the specific answer; the resolved origin is the
 * fallback, and both are loopback.
 */
export function fatalEngineOrigin(supervisor, search = '') {
	const port = Number(supervisor?.lock_port ?? 0);
	if (Number.isInteger(port) && port > 0) {
		return `http://127.0.0.1:${port}`;
	}
	return resolveEngineOrigin(search, globalThis.OPENDJ_ENGINE_ORIGIN);
}

export function setButtonStatus(id, tone, message) {
	const el = byId(id);
	el.dataset.tone = tone;
	el.textContent = message;
}

function wireRelaunchButton(supervisor, search, navigate = defaultNavigate) {
	const button = byId('relaunch');
	const engineOrigin = fatalEngineOrigin(supervisor, search);
	button.addEventListener('click', () => {
		// React FIRST, before any await: the press itself must be visible
		// even if every call below fails.
		button.disabled = true;
		void relaunchEngine({
			healthPort: supervisor?.health_port,
			engineOrigin,
			setStatus: (tone, message) => setButtonStatus('relaunch-status', tone, message)
		}).then((result) => {
			if (result.ok) {
				navigate(engineOrigin);
			} else {
				button.disabled = false;
			}
		});
	});
}

function defaultNavigate(origin) {
	globalThis.location.replace(origin);
}

// Auto-start only in a real page, never when imported by a test runner.
if (typeof document !== 'undefined' && document.getElementById('root') !== null) {
	const search = globalThis.location?.search ?? '';
	const fatal = parseFatalQuery(search);
	if (fatal !== null) {
		showFatal(fatal);
		wireRelaunchButton(fatal, search);
	} else if (globalThis.__OPENDJ_ENGINE_SUPERVISOR__?.engine === 'dead') {
		const supervisor = globalThis.__OPENDJ_ENGINE_SUPERVISOR__;
		showFatal(supervisor);
		wireRelaunchButton(supervisor, search);
	} else {
		startBootstrap({
			origin: resolveEngineOrigin(search, globalThis.OPENDJ_ENGINE_ORIGIN)
		});
	}
}
