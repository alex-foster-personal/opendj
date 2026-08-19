/**
 * WHAT AM I? -- the two identities behind this window, read at runtime.
 *
 * ORIGIN OF THIS MODULE. A tester spent real attention discovering that an
 * installed bake-off app was a stale build from a dirty tree. Nothing on
 * screen could have told him: a good build and a bad one looked identical.
 * So the artifact now answers the question itself, on the main surface, and
 * it answers it from data rather than from a string somebody remembered to
 * update.
 *
 * TWO IDENTITIES, NOT ONE. The desktop shell and the engine are separate
 * artifacts and they drift:
 *
 *   SHELL   -- the Tauri binary. Stamps itself at compile time and injects
 *              `globalThis.OPENDJ_SHELL_BUILD` before any page script runs.
 *              Absent in a browser, which is not a fault: a browser tab has
 *              no shell.
 *   ENGINE  -- the daemon serving this page. Answers GET /api/v1/build-info,
 *              reading a bundled payload manifest when it is packaged and
 *              live git when it is running from a checkout. The `source`
 *              field says which.
 *
 * A shell pointed at a dev engine is exactly the drift this readout exists to
 * expose, so the two are shown side by side and compared rather than merged.
 *
 * NO FALLBACKS, ANYWHERE. Every unknown is rendered as a named fault. A
 * hardcoded default sha, a "dev" placeholder or a blank would each turn "I
 * cannot tell you what I am" into something that looks like an answer, which
 * is the exact failure this replaces. The dirty marker is likewise never
 * suppressed: a build that cannot state a clean provenance must look
 * different from one that can.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 readShellBuild: a stamped shell yields a stamp, an unstamped one
 *     yields an explicit unstamped fault, a browser yields absent.
 *     [if] a missing OPENDJ_SHELL_BUILD reads as a valid identity [then ⛔️] broken
 *   ✔︎ 🎯 fetchEngineBuild: 200 yields the identity, any other outcome yields
 *     a fault carrying the reason, never a blank.
 *     [if] a 503 renders as an empty readout [then ⛔️] broken
 *   ✔︎ 🎯 formatStamp: one instant rendered as local AND UTC, unparseable
 *     input refused rather than shown as "Invalid Date".
 *     [if] "not-a-date" renders as a date [then ⛔️] broken
 *   ✔︎ 🎯 describeDrift: same sha on both sides is aligned, different shas is
 *     drifted, and an unknown side is neither.
 *     [if] an unknown engine sha reports "aligned" [then ⛔️] broken
 */

import { API_BASE } from '$lib/api/client';

/** The route both daemons' engine build serves. Spelled once. */
export const BUILD_INFO_PATH = '/api/v1/build-info';

/** Where the Tauri shell injects its own compile-time stamp. */
export const SHELL_BUILD_GLOBAL = 'OPENDJ_SHELL_BUILD';

/** Shown wherever a value is genuinely unknown. Never a plausible-looking value. */
export const UNKNOWN = '?';

/** The shell's compile-time stamp, exactly as `main.rs` injects it. */
export interface ShellStamp {
	stamped: boolean;
	app_version: string | null;
	git_sha: string | null;
	git_sha_full: string | null;
	git_branch: string | null;
	git_dirty: boolean | null;
	built_at_utc: string | null;
	lane_label: string | null;
}

/** The engine's answer, mirroring `BuildInfoOut` in apps/engine_core/build_info.py. */
export interface EngineStamp {
	source: 'payload' | 'repo';
	engine_version: string;
	git_sha: string;
	git_sha_full: string;
	git_branch: string;
	git_dirty: boolean;
	built_at_utc: string;
	built_at_kind: 'payload-build' | 'head-commit';
	lane_label: string | null;
	product_name: string | null;
	bundle_identifier: string | null;
	app_version: string | null;
	manifest_path: string | null;
}

/**
 * One side's state. `absent` is a fact about the environment (no shell in a
 * browser); `fault` is a failure to answer and is always rendered.
 */
export type SideState<T> =
	| { kind: 'ok'; value: T }
	| { kind: 'absent'; reason: string }
	| { kind: 'fault'; reason: string }
	| { kind: 'loading' };

// ----- shell --------------------------------------------------------------
/**
 * Read the shell stamp off the global the Tauri shell injected.
 *
 * Three outcomes and no fourth. A shell that compiled without its git stamp
 * reports `stamped: false`, and that is a FAULT rather than an absence: the
 * binary exists and still cannot say what it is, which is precisely the
 * condition that wasted a tester's afternoon.
 */
export function readShellBuild(scope: Record<string, unknown> = globalThis): SideState<ShellStamp> {
	const raw = scope[SHELL_BUILD_GLOBAL];
	if (raw === undefined || raw === null) {
		return {
			kind: 'absent',
			reason: `no ${SHELL_BUILD_GLOBAL} on this page; it is served to a browser, not to the desktop shell`
		};
	}
	if (typeof raw !== 'object') {
		return { kind: 'fault', reason: `${SHELL_BUILD_GLOBAL} is ${typeof raw}, expected an object` };
	}
	const stamp = raw as Partial<ShellStamp>;
	if (stamp.stamped !== true || typeof stamp.git_sha !== 'string' || stamp.git_sha === '') {
		return {
			kind: 'fault',
			reason:
				'the desktop shell was compiled without its git stamp, so it cannot say which commit it is'
		};
	}
	return {
		kind: 'ok',
		value: {
			stamped: true,
			app_version: stamp.app_version ?? null,
			git_sha: stamp.git_sha,
			git_sha_full: stamp.git_sha_full ?? null,
			git_branch: stamp.git_branch ?? null,
			git_dirty: typeof stamp.git_dirty === 'boolean' ? stamp.git_dirty : null,
			built_at_utc: stamp.built_at_utc ?? null,
			lane_label: stamp.lane_label ?? null
		}
	};
}

// ----- engine -------------------------------------------------------------
/**
 * Ask the engine what it is.
 *
 * Hand-rolled rather than routed through the generated client on purpose:
 * this is the one request that must still produce a readable answer when the
 * daemon is a build that predates the route, and `ApiError` would flatten a
 * 404 and a dead socket into the same thrown shape. Here they are different
 * sentences, because they mean different things to whoever is reading.
 */
export async function fetchEngineBuild(
	fetchImpl: typeof fetch = globalThis.fetch,
	base: string = API_BASE
): Promise<SideState<EngineStamp>> {
	let response: Response;
	try {
		response = await fetchImpl(`${base}${BUILD_INFO_PATH}`, {
			headers: { accept: 'application/json' },
			cache: 'no-store'
		});
	} catch (err) {
		const detail = err instanceof Error ? err.message : String(err);
		return { kind: 'fault', reason: `engine unreachable at ${BUILD_INFO_PATH} (${detail})` };
	}
	if (response.status === 404) {
		return {
			kind: 'fault',
			reason: `this daemon does not serve ${BUILD_INFO_PATH}; it predates the build stamp`
		};
	}
	let body: unknown;
	try {
		body = await response.json();
	} catch {
		return {
			kind: 'fault',
			reason: `${BUILD_INFO_PATH} answered ${response.status} with a body that is not JSON`
		};
	}
	if (!response.ok) {
		const envelope = body as { message?: string; error?: string } | null;
		const message = envelope?.message ?? envelope?.error ?? `HTTP ${response.status}`;
		return { kind: 'fault', reason: `the engine cannot state its build identity: ${message}` };
	}
	const stamp = body as Partial<EngineStamp>;
	if (typeof stamp.git_sha !== 'string' || typeof stamp.built_at_utc !== 'string') {
		return {
			kind: 'fault',
			reason: `${BUILD_INFO_PATH} answered 200 without git_sha and built_at_utc`
		};
	}
	return { kind: 'ok', value: stamp as EngineStamp };
}

// ----- rendering helpers --------------------------------------------------
export interface Stamp {
	/** e.g. "Wed 19 Aug 2026, 14:03" in the reader's own zone. */
	local: string;
	/** The same instant in UTC, ISO-ish and unambiguous. */
	utc: string;
}

/**
 * Render one instant twice: the reader's local time AND UTC.
 *
 * Both, because each answers a different question. Local is what a tester
 * compares against "when did you say you pushed it"; UTC is what matches the
 * commit timestamp and every log line. Showing only one makes the other a
 * mental arithmetic problem at exactly the moment somebody is confused.
 */
export function formatStamp(iso: string | null, timeZone?: string): Stamp | null {
	if (iso === null || iso === '') return null;
	const when = new Date(iso);
	if (Number.isNaN(when.getTime())) return null;
	const local = new Intl.DateTimeFormat('en-GB', {
		weekday: 'short',
		day: '2-digit',
		month: 'short',
		year: 'numeric',
		hour: '2-digit',
		minute: '2-digit',
		hour12: false,
		...(timeZone === undefined ? {} : { timeZone })
	}).format(when);
	return { local, utc: `${when.toISOString().slice(0, 16).replace('T', ' ')}Z` };
}

export type Drift = 'aligned' | 'drifted' | 'unknown';

/**
 * Do the two halves of this app come from the same commit?
 *
 * `unknown` is a real answer and never collapses into `aligned`. A browser
 * tab has no shell to compare, and an engine that cannot state its sha has
 * not agreed with anything.
 */
export function describeDrift(
	shell: SideState<ShellStamp>,
	engine: SideState<EngineStamp>
): Drift {
	if (shell.kind !== 'ok' || engine.kind !== 'ok') {
		return 'unknown';
	} else if (shell.value.git_sha_full === null || engine.value.git_sha_full === '') {
		return 'unknown';
	} else if (shell.value.git_sha_full === engine.value.git_sha_full) {
		return 'aligned';
	}
	return 'drifted';
}

/** The one-line label for a side, or the fault sentence that replaces it. */
export function shortLabel(state: SideState<ShellStamp | EngineStamp>): string {
	if (state.kind === 'loading') return 'reading...';
	if (state.kind !== 'ok') return UNKNOWN;
	const value = state.value;
	const dirty = value.git_dirty === true ? ' DIRTY' : '';
	return `${value.git_sha ?? UNKNOWN}${dirty}`;
}

/**
 * The hover explanation, per house rule: every readout says what it is and
 * where the number came from.
 */
export function explainSide(
	side: 'shell' | 'engine',
	state: SideState<ShellStamp | EngineStamp>
): string {
	const provenance =
		side === 'shell'
			? `Desktop shell identity, baked into the Tauri binary at compile time and injected as globalThis.${SHELL_BUILD_GLOBAL}.`
			: `Engine identity, read at runtime from GET ${BUILD_INFO_PATH} -- the bundled payload's manifest.json in a packaged app, live git in a source checkout.`;
	if (state.kind === 'ok') {
		const value = state.value;
		const parts = [
			provenance,
			`commit ${value.git_sha_full ?? value.git_sha ?? UNKNOWN}`,
			`branch ${value.git_branch ?? UNKNOWN}`,
			`lane ${value.lane_label ?? '(none)'}`,
			value.git_dirty === true
				? 'DIRTY: uncommitted changes were present when this was built, so the commit above does not fully describe it.'
				: 'clean tree at build time'
		];
		if ('source' in value) {
			parts.push(
				value.source === 'payload'
					? `packaged: identity from ${value.manifest_path ?? 'the payload manifest'}`
					: 'running from a source checkout: identity from live git, not from a shipped manifest'
			);
		}
		return parts.join('\n');
	}
	return `${provenance}\n\nNo identity available: ${state.kind === 'loading' ? 'still reading' : state.reason}`;
}
