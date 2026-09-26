/**
 * The copyable identity of one toast: its id, when it happened, and what was
 * running when it did.
 *
 * ORIGIN. A toast said "set deck master failed", faded after five seconds, and
 * could not be dismissed. By the time anyone asked what it said, the only
 * durable trace was a perf-event ring row that carried the message and NO way
 * to tell WHICH raising of that message it belonged to. Two identical toasts
 * ten minutes apart produced two identical rows.
 *
 * THE ONE RULE THIS MODULE EXISTS TO ENFORCE. The id a person copies off the
 * screen must be the id the log line already carries. Not a display-only id
 * minted when the toast is rendered, and not a fresh id minted at copy time -
 * either of those reads as a correlation key and leads nowhere, which is worse
 * than printing no id at all because it costs the reader a search before they
 * learn it was never going to work. `pushToast` mints the id ONCE, before it
 * writes either log, and stamps the same string into both.
 *
 * WHY THE ID CARRIES A SESSION TOKEN. The pre-existing toast id was a counter
 * that restarted at 1 on every page load, so `toast_id: 3` in a week of JSONL
 * matched every third toast of every session. A per-load random token makes one
 * id greppable to one raising, which is the entire point of printing it.
 *
 * PURE ON PURPOSE. Nothing here reads a store, a rune, or the network, so the
 * payload builder is testable by execution rather than by reading the source.
 * The caller gathers the environment and hands it in.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 formatToastId: one id per raising, greppable, stable across the
 *     screen, the ring row and the server report.
 *     [if] two toasts in one session share an id [then ⛔️] broken
 *     [if] the id has no session token and so collides across reloads [then ⛔️] broken
 *   ✔︎ ✅ 🎯 describeToastClient: a Tauri shell reports the app and its version,
 *     a browser reports the browser and its version, an unrecognized agent
 *     reports itself verbatim rather than guessing.
 *     [if] an unknown user agent renders as "Safari" [then ⛔️] broken
 *   ✔︎ ✅ 🎯 buildToastReport: carries id, timestamp, message, machine, user,
 *     client and version, and nothing else.
 *     [if] the payload omits the id [then ⛔️] broken
 *   ✔︎ ✅ 🎯 writeToastReport: an unavailable clipboard throws a named error.
 *     [if] a missing clipboard API resolves silently [then ⛔️] broken
 */

/** Prefix every toast id carries, so `t-` is enough to find one in a log. */
export const TOAST_ID_PREFIX = 't';

/** Value used where a fact is genuinely not knowable, never a plausible guess. */
export const UNKNOWN = 'unknown';

/**
 * A short random token identifying this page load.
 *
 * Short because a human reads it off the screen and types it into a search box;
 * random because the alternative (a counter) is what made the previous id
 * useless across reloads. Six base36 characters is ~2.2e9 sessions, which is
 * far past the collision risk that matters for one DJ's log directory.
 */
export function newToastSessionToken(
	randomSource: { getRandomValues?: (array: Uint32Array) => Uint32Array } | undefined =
		typeof crypto === 'undefined' ? undefined : crypto
): string {
	if (randomSource?.getRandomValues !== undefined) {
		const buffer = randomSource.getRandomValues(new Uint32Array(1));
		return buffer[0].toString(36).padStart(6, '0').slice(-6);
	}
	// Same shape of fallback client-error-reporting.ts already uses for its
	// event id: a host with no crypto still produces a usable, distinct token.
	return Math.floor(Math.random() * 0xffffffff)
		.toString(36)
		.padStart(6, '0')
		.slice(-6);
}

/** `t-k3f9a2-7`. One toast raising, findable by substring in any log. */
export function formatToastId(sessionToken: string, sequence: number): string {
	if (sessionToken === '') throw new Error('formatToastId: sessionToken must not be empty');
	if (!Number.isInteger(sequence) || sequence < 1) {
		throw new RangeError(`formatToastId: sequence must be a positive integer, got ${sequence}`);
	}
	return `${TOAST_ID_PREFIX}-${sessionToken}-${sequence}`;
}

// ----- what is this running in -------------------------------------------

/** The shell stamp fields this module reads. Structural rather than imported
 * so the module stays off build-identity's dependency edge and off types.ts. */
export interface ShellStampLike {
	app_version?: string | null;
	git_sha?: string | null;
}

export interface ToastClient {
	/** e.g. "Open DJ desktop app" or "Safari". */
	name: string;
	/** e.g. "0.4.1" or "18.1", or UNKNOWN when the agent does not say. */
	version: string;
}

/**
 * Browser families, most specific first.
 *
 * Order is load-bearing and is the whole reason this is a list rather than a
 * map: every Chrome user agent also contains "Safari", and Edge contains both,
 * so a match on the first entry that appears would report Chrome as Safari.
 * Each entry names the token that must be ABSENT for the match to count.
 */
const BROWSER_RULES: readonly { name: string; token: string; notIf: readonly string[] }[] = [
	{ name: 'Edge', token: 'Edg/', notIf: [] },
	{ name: 'Chrome', token: 'Chrome/', notIf: ['Edg/'] },
	{ name: 'Firefox', token: 'Firefox/', notIf: [] },
	{ name: 'Safari', token: 'Version/', notIf: ['Chrome/', 'Edg/'] }
];

function _versionAfter(userAgent: string, token: string): string {
	const at = userAgent.indexOf(token);
	if (at === -1) return UNKNOWN;
	const rest = userAgent.slice(at + token.length);
	const match = /^[0-9][0-9._]*/.exec(rest);
	return match === null ? UNKNOWN : match[0];
}

/**
 * Which client is this, and which version of it.
 *
 * The packaged app wins over the user agent, because the Tauri shell renders in
 * a WebKit webview whose user agent says "Safari" - reporting that would tell
 * the reader the opposite of the fact they asked for. The shell stamp is
 * present only in the packaged app, so its presence IS the answer.
 *
 * An unrecognized user agent is reported verbatim rather than bucketed into the
 * nearest known family. A wrong client name sends whoever is reading the report
 * to reproduce the bug on the wrong surface.
 */
export function describeToastClient(input: {
	shellStamp?: ShellStampLike | null;
	userAgent?: string | null;
}): ToastClient {
	const shell = input.shellStamp;
	if (shell !== undefined && shell !== null) {
		return {
			name: 'Open DJ desktop app',
			version: shell.app_version ?? shell.git_sha ?? UNKNOWN
		};
	}
	const userAgent = (input.userAgent ?? '').trim();
	if (userAgent === '') return { name: UNKNOWN, version: UNKNOWN };
	for (const rule of BROWSER_RULES) {
		if (!userAgent.includes(rule.token)) continue;
		if (rule.notIf.some((token) => userAgent.includes(token))) continue;
		return { name: rule.name, version: _versionAfter(userAgent, rule.token) };
	}
	// Verbatim, clipped. "I do not recognize this" is a better report than a
	// confident wrong family, and the raw string is what identifies it.
	return { name: userAgent.slice(0, 120), version: UNKNOWN };
}

// ----- the payload --------------------------------------------------------

/**
 * The environment facts the report carries. This list is CLOSED on purpose:
 * exactly what was asked for (machine, user, client, version) and nothing that
 * happens to be reachable. A report a person pastes into an issue must not
 * quietly carry a token, a path or anything else from the host.
 */
export interface ToastEnvironment {
	machine: string;
	user: string;
	client: ToastClient;
	/** The page the toast was raised on. Origin plus path, never the query. */
	url: string;
}

export interface ToastReportInput {
	id: string;
	kind: 'info' | 'warn' | 'error';
	/** Human headline shown on screen. */
	headline: string;
	/** Raw message written to the perf-event ring. */
	message: string;
	detail?: string | undefined;
	classification?: string | undefined;
	settingsSummary?: string | undefined;
	hint?: string | undefined;
	stack?: string | undefined;
	clientEventId?: string | undefined;
	serverEventId?: string | undefined;
	errorId?: string | undefined;
	sentryEventId?: string | undefined;
	/** ISO 8601 UTC, and the SAME instant the log row carries. */
	createdAt: string;
	env: ToastEnvironment;
	/** Optional context lines appended after the closed env block (deck/sync, caller context). */
	extras?: Record<string, string>;
}

/**
 * The text that lands on the clipboard.
 *
 * Plain `key: value` lines rather than JSON: this gets pasted into a GitHub
 * issue and a chat message, where JSON braces render as noise and a reader
 * skimming for the id has to parse past them.
 *
 * `id` is FIRST because it is the only line that lets the reader find the rest
 * of the story in the logs.
 */
export function buildToastReport(input: ToastReportInput): string {
	const { env } = input;
	const lines = [
		`id: ${input.id}`,
		...(input.classification !== undefined ? [`classification: ${input.classification}`] : []),
		...(input.clientEventId !== undefined ? [`client_event_id: ${input.clientEventId}`] : []),
		...(input.serverEventId !== undefined ? [`server_event_id: ${input.serverEventId}`] : []),
		...(input.errorId !== undefined ? [`error_id: ${input.errorId}`] : []),
		...(input.sentryEventId !== undefined ? [`sentry_event_id: ${input.sentryEventId}`] : []),
		`when: ${input.createdAt}`,
		`kind: ${input.kind}`,
		`headline: ${input.headline}`,
		`message: ${input.message}`,
		...(input.detail !== undefined && input.detail !== input.headline
			? [`detail: ${input.detail}`]
			: []),
		...(input.settingsSummary !== undefined ? [`settings: ${input.settingsSummary}`] : []),
		...(input.hint !== undefined ? [`hint: ${input.hint}`] : []),
		...(input.stack !== undefined ? [`stack: ${input.stack}`] : []),
		`machine: ${env.machine}`,
		`user: ${env.user}`,
		`client: ${env.client.name} ${env.client.version}`,
		`page: ${env.url}`,
		...(input.extras === undefined
			? []
			: Object.entries(input.extras).map(([key, value]) => `${key}: ${value}`)),
		`find in logs: search ${input.id}`
	];
	return lines.join('\n');
}

// ----- the clipboard ------------------------------------------------------

/** The clipboard could not be written. Named so a caller can raise it visibly
 * rather than letting a copy that did nothing look like a copy that worked. */
export class ClipboardUnavailableError extends Error {
	constructor(reason: string) {
		super(`clipboard unavailable: ${reason}`);
		this.name = 'ClipboardUnavailableError';
	}
}

/** The one clipboard method this module needs. Keeps it callable from
 * node:test, where there is no navigator. */
export interface ClipboardLike {
	writeText(text: string): Promise<void>;
}

/**
 * Write the report, or throw saying why not.
 *
 * NO SILENT CATCH, by house rule and because of the specific failure this
 * replaces: a copy that quietly does nothing is indistinguishable from a copy
 * that worked until the user pastes into an issue and gets whatever was on the
 * clipboard before. Both reasons a clipboard write fails here are things the
 * reader can act on, so both are named:
 *
 *   INSECURE ORIGIN. navigator.clipboard is undefined outside a secure context.
 *     localhost and 127.0.0.1 count as secure even over plain http; a bare LAN
 *     IP over http does not, which is exactly how the packaged app gets reached
 *     from another machine on the network.
 *   REFUSED. The API exists and rejected, e.g. the write did not happen inside
 *     a user gesture, or the browser denied permission.
 */
export async function writeToastReport(
	text: string,
	clipboard: ClipboardLike | undefined,
	isSecureContext: boolean
): Promise<void> {
	if (clipboard === undefined || typeof clipboard.writeText !== 'function') {
		throw new ClipboardUnavailableError(
			isSecureContext
				? 'this browser exposes no navigator.clipboard.writeText'
				: 'this page is not a secure context, so the clipboard API is not available. ' +
					'Reach the app on localhost or 127.0.0.1 rather than a bare IP over http'
		);
	}
	try {
		await clipboard.writeText(text);
	} catch (cause) {
		const detail = cause instanceof Error ? cause.message : String(cause);
		throw new ClipboardUnavailableError(`the browser refused the write (${detail})`);
	}
}
