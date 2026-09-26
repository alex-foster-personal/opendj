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
 *   ✔︎ 🎯 engineBaseUrl: an engine-served page reports its own origin, a
 *     configured VITE_API_BASE reports that instead, and neither available is
 *     a fault rather than a guessed port.
 *     [if] a page with no origin and no configured base prints a plausible
 *     127.0.0.1 address [then ⛔️] broken
 */

import { API_BASE } from '$lib/api/base';

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
	release_channel: string | null;
	evidence_written_at_utc: string | null;
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
	built_at_kind: 'payload-build' | 'head-commit' | 'engine-start';
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
			lane_label: stamp.lane_label ?? null,
			release_channel:
				typeof stamp.release_channel === 'string' && stamp.release_channel !== ''
					? stamp.release_channel
					: null,
			evidence_written_at_utc:
				typeof stamp.evidence_written_at_utc === 'string' &&
				stamp.evidence_written_at_utc !== ''
					? stamp.evidence_written_at_utc
					: null
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

const HOUR_MS = 3_600_000;

/**
 * How long ago a build was stamped, in whole days and hours ("1d 5h", "5h",
 * "<1h"): a stale build should read as stale at a glance, which a calendar
 * date does not. A stamp in the future (a skewed clock) or an unparseable one
 * has NO age rather than a negative or zero one.
 */
export function formatAge(iso: string | null, now: Date): string | null {
	if (iso === null || iso === '') return null;
	const ageMs = now.getTime() - new Date(iso).getTime();
	if (Number.isNaN(ageMs) || ageMs < 0) return null;
	const hours = Math.floor(ageMs / HOUR_MS);
	if (hours === 0) return '<1h';
	if (hours < 24) return `${hours}h`;
	return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

// ----- where this app is --------------------------------------------------
/** The subset of `Location` this module reads. Keeps the function callable
 * from node:test, where there is no DOM. */
export interface LocationLike {
	origin?: string;
	href?: string;
}

/** How the base URL was arrived at. Different fact, different sentence. */
export type EngineUrlSource = 'served' | 'configured';

export type EngineUrl =
	| { kind: 'ok'; url: string; source: EngineUrlSource }
	| { kind: 'fault'; reason: string };

function _trimTrailingSlash(url: string): string {
	return url.endsWith('/') ? url.slice(0, -1) : url;
}

/**
 * The engine's base URL, in the form a person can paste into a browser.
 *
 * ORIGIN OF THIS FUNCTION. The packaged app starts its engine on an
 * OS-ASSIGNED free loopback port, so the address is different every launch and
 * is baked into nothing. A tester who wanted to open the app in a real browser
 * had no way to find it, guessed the dev port, and got nothing. The window
 * knows the answer; it just never said it out loud. Now it does.
 *
 * TWO SOURCES, NAMED. `served` means this page came FROM the engine, so the
 * page's own origin IS the engine: that is the packaged app, and the port in
 * it is the ephemeral one. `configured` means `VITE_API_BASE` pointed the dev
 * server at a daemon somewhere else, in which case the page's origin is the
 * Vite server and would be the wrong answer.
 *
 * NO FALLBACK. A relative `VITE_API_BASE` with no document to resolve it
 * against, or a page with no origin, is a fault sentence. Printing a plausible
 * `http://127.0.0.1:8585` would send the reader to a port nothing is on, which
 * is the exact failure this exists to end.
 */
export function engineBaseUrl(
	location: LocationLike | null = (globalThis as { location?: LocationLike }).location ?? null,
	apiBase: string = API_BASE
): EngineUrl {
	const configured = apiBase.trim();
	if (configured !== '') {
		try {
			const parsed = new URL(configured);
			return {
				kind: 'ok',
				url: _trimTrailingSlash(`${parsed.origin}${parsed.pathname}`),
				source: 'configured'
			};
		} catch {
			// Not absolute. It can only be resolved against this document.
			if (location?.href === undefined || location.href === '') {
				return {
					kind: 'fault',
					reason: `VITE_API_BASE is "${configured}", which is relative, and there is no document to resolve it against`
				};
			}
			try {
				const parsed = new URL(configured, location.href);
				return {
					kind: 'ok',
					url: _trimTrailingSlash(`${parsed.origin}${parsed.pathname}`),
					source: 'configured'
				};
			} catch (err) {
				const detail = err instanceof Error ? err.message : String(err);
				return {
					kind: 'fault',
					reason: `VITE_API_BASE is "${configured}", which is not a usable URL (${detail})`
				};
			}
		}
	}
	if (location?.origin === undefined || location.origin === '' || location.origin === 'null') {
		return {
			kind: 'fault',
			reason:
				'this page has no origin, so the engine that served it cannot be named; VITE_API_BASE is unset'
		};
	}
	return { kind: 'ok', url: _trimTrailingSlash(location.origin), source: 'served' };
}

/** The hover explanation for the base URL readout. */
export function explainEngineUrl(state: EngineUrl): string {
	if (state.kind === 'fault') {
		return `The engine's address cannot be determined: ${state.reason}`;
	}
	const provenance =
		state.source === 'served'
			? "This page was served BY the engine, so the engine is this window's own origin. The packaged app picks a free loopback port at every launch, which is why this number changes between runs."
			: 'VITE_API_BASE points this dev build at a daemon that did not serve this page, so the address is the configured one and not this page\'s origin.';
	return `${provenance}\n\nOpen ${state.url} in a browser to reach the same app.`;
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
export interface BuildIdentityReportInput {
	shell: SideState<ShellStamp>;
	engine: SideState<EngineStamp>;
	engineUrl: EngineUrl;
	drift: Drift;
	updateSummary: { label: string; title: string } | null;
}

/**
 * Plain-text build identity for clipboard and agent parity.
 *
 * Agents may assemble the same shape from GET /api/v1/build-info, the shell
 * global, and the page origin; the UI uses this function so humans and agents
 * share one report layout.
 */
export function formatBuildIdentityReport(input: BuildIdentityReportInput): string {
	const lines: string[] = [];
	if (input.engineUrl.kind === 'ok') {
		lines.push(`url: ${input.engineUrl.url}`);
		lines.push(
			`url_provenance: ${input.engineUrl.source === 'served' ? 'page origin (engine-served)' : 'VITE_API_BASE (configured daemon)'}`
		);
	} else {
		lines.push(`url: ${UNKNOWN}`);
		lines.push(`url_provenance: ${explainEngineUrl(input.engineUrl)}`);
	}
	lines.push(`drift: ${input.drift}`);
	const channel =
		input.shell.kind === 'ok' && input.shell.value.release_channel
			? input.shell.value.release_channel
			: UNKNOWN;
	lines.push(`channel: ${channel}`);
	if (input.engine.kind === 'ok') {
		lines.push(
			`app_version_engine: ${input.engine.value.product_name ?? UNKNOWN} · ${input.engine.value.bundle_identifier ?? UNKNOWN} · v${input.engine.value.app_version ?? UNKNOWN}`
		);
	} else {
		lines.push(`app_version_engine: ${explainSide('engine', input.engine)}`);
	}
	if (input.shell.kind === 'absent') {
		lines.push('app_version_shell: browser identity · Chrome dev loop');
	} else if (input.shell.kind === 'ok') {
		lines.push(`app_version_shell: DMG app · v${input.shell.value.app_version ?? UNKNOWN}`);
	} else {
		lines.push(`app_version_shell: ${explainSide('shell', input.shell)}`);
	}
	lines.push('engine:');
	if (input.engine.kind === 'ok') {
		const e = input.engine.value;
		lines.push(`  git_sha: ${e.git_sha_full}`);
		lines.push(`  git_branch: ${e.git_branch ?? UNKNOWN}`);
		lines.push(`  source: ${e.source}`);
		lines.push(`  engine_version: v${e.engine_version}`);
		lines.push(`  dirty: ${e.git_dirty ? 'yes' : 'no'}`);
		const stamp = formatStamp(e.built_at_utc);
		if (stamp !== null) {
			lines.push(`  built_at_local: ${stamp.local}`);
			lines.push(`  built_at_utc: ${stamp.utc}`);
		}
	} else {
		lines.push(`  ${explainSide('engine', input.engine)}`);
	}
	lines.push('shell:');
	if (input.shell.kind === 'ok') {
		const s = input.shell.value;
		lines.push(`  git_sha: ${s.git_sha_full ?? s.git_sha ?? UNKNOWN}`);
		lines.push(`  git_branch: ${s.git_branch ?? UNKNOWN}`);
		lines.push(`  dirty: ${s.git_dirty === true ? 'yes' : s.git_dirty === false ? 'no' : UNKNOWN}`);
		const stamp = formatStamp(s.built_at_utc);
		if (stamp !== null) {
			lines.push(`  built_at_local: ${stamp.local}`);
			lines.push(`  built_at_utc: ${stamp.utc}`);
		}
	} else {
		lines.push(`  ${explainSide('shell', input.shell)}`);
	}
	if (input.updateSummary !== null) {
		lines.push(`update: ${input.updateSummary.label}`);
	}
	return lines.join('\n');
}

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
		if ('release_channel' in value) {
			parts.push(`channel ${value.release_channel ?? '(none)'}`);
			if (value.evidence_written_at_utc) {
				parts.push(`evidence ${value.evidence_written_at_utc}`);
			}
		}
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
