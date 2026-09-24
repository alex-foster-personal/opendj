/** Runtime-lite shared state for banner warnings + toast stack.
 * Uses Svelte 5 runes so components can `$derive` and re-render cheaply.
 */
import { getHealth, getSettings, type HealthOut } from './api';
import {
	entryFromHealth,
	pushHistory,
	type HealthHistoryEntry
} from '../routes/admin/health-history';
import { auth } from './auth.svelte';
import { onClientErrorAck, reportClientError, type ClientErrorContext } from './client-error-reporting';
import {
	formatToastPresentation,
	type ToastPresentation
} from './toast-presentation';
import { readShellBuild } from './rb/build-identity';
import { mintErrorId } from './rb/error-id';
import { recordPerfEvent } from './rb/perf-event-log';
import {
	buildToastReport,
	describeToastClient,
	formatToastId,
	newToastSessionToken,
	UNKNOWN,
	writeToastReport,
	type ToastEnvironment
} from './toast-report';

/**
 * `logId` is the correlation key and the only id that leaves this module: it is
 * printed on screen, copied to the clipboard, written into the perf-event ring
 * row and sent as `toast_id` on the server report. `id` stays a plain counter
 * because Svelte's keyed `{#each}` wants a cheap stable key and nothing else
 * reads it.
 *
 * `createdAt` is taken FROM the ring row rather than measured again here, so
 * the timestamp on the clipboard and the timestamp in the log are the same
 * instant rather than two readings a millisecond apart.
 */
export type ToastAction = {
	label: string;
	handler: () => void;
};

export type Toast = {
	id: number;
	logId: string;
	message: string;
	headline: string;
	detail?: string;
	solutionHint?: string;
	expanded?: boolean;
	clientEventId?: string;
	serverEventId?: string;
	errorId?: string;
	sentryEventId?: string;
	action?: ToastAction;
	/**
	 * `warn` is the middle rung, added for pin 9bf12adccb45: a BAR beat sync
	 * that had to fold to half/double tempo now HAPPENS and says so in orange,
	 * where it used to be refused in red. An outcome the DJ should see but that
	 * did not fail has no honest home in a two-value scale - it either
	 * overstates as an error or disappears as info.
	 */
	kind: 'info' | 'warn' | 'error';
	createdAt: string;
	count: number;
	groupKey: string | undefined;
};

// The numeric counter now lives in `rb/error-id.ts` and is shared with the deck
// error banner. The session token stays here because it scopes THIS page load,
// which is what stops `t-3` matching the third id of every session ever.
const _toastSession = newToastSessionToken();
export const toasts = $state<Toast[]>([]);

onClientErrorAck((ack) => {
	for (const toast of toasts) {
		if (toast.clientEventId !== ack.client_event_id) continue;
		toast.serverEventId = ack.server_event_id;
		if (ack.error_id) toast.errorId = ack.error_id;
		if (ack.sentry_event_id) toast.sentryEventId = ack.sentry_event_id;
	}
});
// Retain occurrence ids only while their grouped toast remains actionable.
const _toastLogIds = new Map<number, Set<string>>();

/**
 * The live dismissal timer per toast, plus the delay to restart it with.
 *
 * Held here rather than in the component because dismissal must survive the
 * component re-rendering, and because an agent driving the IPC surface has to
 * be able to pause and dismiss without a pointer.
 */
const _timers = new Map<
	number,
	{ handle: ReturnType<typeof setTimeout> | null; dismissMs: number }
>();

/**
 * Stop the countdown, keep the entry.
 *
 * The entry survives because `releaseToast` needs the toast's ORIGINAL delay to
 * restart, and a held toast must still report itself as not-counting-down:
 * `handle: null` is what distinguishes held from armed. Deleting the entry
 * instead would make a held toast indistinguishable from one that was never
 * pushed, and `timer_armed` over the IPC would answer an agent wrongly.
 */
function _clearTimer(id: number): void {
	const timer = _timers.get(id);
	if (timer === undefined) return;
	if (timer.handle !== null) clearTimeout(timer.handle);
	_timers.set(id, { handle: null, dismissMs: timer.dismissMs });
}

/** Start (or restart) the full dismissal delay for one toast. */
function _armTimer(id: number, dismissMs: number): void {
	_clearTimer(id);
	_timers.set(id, {
		handle: setTimeout(() => {
			_timers.delete(id);
			_removeToast(id);
		}, dismissMs),
		dismissMs
	});
}

function _removeToast(id: number): void {
	_toastLogIds.delete(id);
	const i = toasts.findIndex((t) => t.id === id);
	if (i >= 0) toasts.splice(i, 1);
}

function _find(logId: string): Toast | undefined {
	return toasts.find((t) => t.logId === logId || _toastLogIds.get(t.id)?.has(logId));
}

/** Default auto-dismiss delay when a caller does not name its own. */
export const TOAST_DEFAULT_MS = 5000;

/**
 * Every toast outlives its own on-screen dismissal in the perf-event-log
 * ring (localStorage + console) - toasts vanish after TOAST_DEFAULT_MS with
 * no other trace, which is exactly what made the "why didn't AutoPlay fire"
 * investigation on Mon 17 Aug 2026 into log archaeology instead of a lookup.
 * One chokepoint here covers every current and future pushToast call site.
 *
 * `context` rides the error report this toast already sends. An error toast is
 * the client's only route to the server-side client-error log, so a caller that
 * measured WHY it is raising the toast (deck load stage timings, say) attaches
 * it here rather than firing a second reportClientError of its own - two
 * reports per failure would land as two JSONL rows with the diagnosis on
 * neither, because reportClientError dedupes on `source`.
 */
export function pushToast(
	message: string,
	kind: 'info' | 'warn' | 'error' = 'info',
	dismissMs: number = TOAST_DEFAULT_MS,
	cause?: unknown,
	context: ClientErrorContext = {},
	groupKey?: string,
	action?: ToastAction,
	presentationOverride?: Partial<ToastPresentation>
): void {
	if (!Number.isFinite(dismissMs) || dismissMs <= 0) {
		throw new RangeError(`pushToast: dismissMs must be a positive finite number, got ${dismissMs}`);
	}
	// P11-F03: capture *this* toast's id in the closure. The previous
	// implementation closed over a module-level counter, which meant
	// overlapping toasts would cause each timer to dismiss the
	// most-recently-pushed toast instead of the one that was actually due
	// to expire.
	//
	// The counter itself now lives in `rb/error-id.ts` and is shared with the
	// deck error banner, which mints from the same source. One counter is what
	// lets a reader holding an id search for it without first having to work
	// out which surface raised it. The sequence therefore SKIPS whenever the
	// banner mints, which is harmless: the id has to be unique and increasing,
	// not gapless.
	const id = mintErrorId();
	// MINTED ONCE, BEFORE EITHER LOG WRITE. This ordering is the whole feature:
	// the ring row, the server report and the on-screen toast all receive the
	// same string, so the id a person copies off the screen is the id they can
	// search for. Minting it after the logs, or again at copy time, would
	// produce an id that looks like a correlation key and matches nothing.
	const logId = formatToastId(_toastSession, id);
	const presentation = {
		...formatToastPresentation({ kind, message, cause, feature: presentationOverride?.feature }),
		...presentationOverride
	};
	const row = recordPerfEvent(
		`toast-${kind}`,
		message,
		null,
		// recordPerfEvent's severity scale is already info/warn/error, so the
		// toast kind maps straight onto it rather than being flattened.
		kind,
		logId
	);
	// Group only when a caller names the same control. Every occurrence still
	// has its own log row; the displayed message/id refer to the latest one.
	const existing = groupKey === undefined ? undefined :
		toasts.find((toast) => toast.groupKey === groupKey && toast.kind === kind);
	const toast =
		existing ??
		({
			id,
			logId,
			message,
			headline: presentation.headline,
			detail: presentation.detail,
			solutionHint: presentation.solutionHint,
			expanded: false,
			kind,
			createdAt: row.t,
			count: 0,
			groupKey
		} satisfies Toast);
	if (groupKey !== undefined) {
		const logIds = _toastLogIds.get(toast.id) ?? new Set<string>();
		logIds.add(logId);
		_toastLogIds.set(toast.id, logIds);
	}
	Object.assign(toast, {
		logId,
		message,
		headline: presentation.headline,
		detail: presentation.detail,
		solutionHint: presentation.solutionHint,
		createdAt: row.t,
		count: toast.count + 1,
		action
	});
	if (existing === undefined) toasts.push(toast);
	// Warm the host lookup now so the eventual click can write the clipboard
	// synchronously inside its own gesture. See _machineName.
	void _machineName();
	if (kind === 'error') {
		// The caller's context is spread last so it can name its own `source`,
		// which is what keeps a deck-load failure filterable in the JSONL
		// instead of anonymous under 'toast'.
		//
		// This report is DEDUPED by fingerprint for 10s upstream, so it is not a
		// reliable home for the id on its own. The ring row above is written
		// unconditionally, which is why that is the surface the id is promised
		// against and this one is the bonus.
		const clientEventId = reportClientError(cause ?? new Error(message), {
			source: 'toast',
			toast_id: logId,
			...context
		});
		if (clientEventId !== undefined) toast.clientEventId = clientEventId;
	}
	if (existing !== undefined && _timers.get(toast.id)?.handle === null) {
		_timers.set(toast.id, { handle: null, dismissMs });
	} else {
		_armTimer(toast.id, dismissMs);
	}
}

// ----- dismissal ----------------------------------------------------------

/**
 * Remove one toast now, by its correlation id.
 *
 * Returns whether it was there. A caller that dismissed nothing (already faded,
 * wrong id) learns so rather than being told the dismissal worked, which is
 * what an agent asserting on this surface needs.
 */
export function dismissToast(logId: string): boolean {
	const toast = _find(logId);
	if (toast === undefined) return false;
	_clearTimer(toast.id);
	_timers.delete(toast.id);
	_removeToast(toast.id);
	return true;
}

/**
 * Hold this toast on screen indefinitely. Called on pointer enter.
 *
 * The timer is CLEARED rather than paused-with-remaining: on leave the full
 * delay restarts, which is what "hover resets timer" asks for. A toast someone
 * is reading must not vanish a tenth of a second after they look away because
 * that is all that was left of its original five seconds.
 */
export function holdToast(logId: string): boolean {
	const toast = _find(logId);
	if (toast === undefined) return false;
	_clearTimer(toast.id);
	return true;
}

/** Restart the full dismissal delay. Called on pointer leave. */
export function releaseToast(logId: string): boolean {
	const toast = _find(logId);
	if (toast === undefined) return false;
	const timer = _timers.get(toast.id);
	_armTimer(toast.id, timer?.dismissMs ?? TOAST_DEFAULT_MS);
	return true;
}

/** Toggle expanded detail for one toast. Returns the new expanded state. */
export function toggleToastExpanded(logId: string): boolean {
	const toast = _find(logId);
	if (toast === undefined) {
		throw new Error(`toggleToastExpanded: no toast with id ${logId} is on screen`);
	}
	toast.expanded = toast.expanded !== true;
	return toast.expanded === true;
}

/** Test/agent seam: is a dismissal timer currently armed for this toast? */
export function toastTimerArmed(logId: string): boolean {
	const toast = _find(logId);
	if (toast === undefined) return false;
	return _timers.get(toast.id)?.handle != null;
}

// ----- copy ---------------------------------------------------------------

/**
 * The host facts the report needs, fetched once and reused.
 *
 * PREFETCHED, NOT FETCHED ON CLICK, and that is a correctness requirement
 * rather than a performance one: WebKit only honors a clipboard write that
 * happens inside the user gesture that triggered it, and an `await fetch()`
 * before the write ends that gesture. Safari is the browser the maintainer reports from,
 * so the network call has to be finished before the click arrives.
 *
 * Kicked off by the first toast of the session; a toast lives seconds and has
 * to be hovered and clicked, so this has resolved long before any copy.
 */
let _machinePromise: Promise<string> | null = null;

function _machineName(): Promise<string> {
	if (_machinePromise === null) {
		_machinePromise = getSettings()
			.then((settings) => {
				for (const group of settings.groups) {
					for (const item of group.items) {
						if (item.key === 'hostname' && typeof item.value === 'string' && item.value !== '') {
							return item.value;
						}
					}
				}
				return UNKNOWN;
			})
			.catch(() => UNKNOWN);
	}
	return _machinePromise;
}

/**
 * Gather the environment. Every field comes from something the app already
 * knows, and the list is closed to exactly what was asked for.
 *
 * `user` is the signed-in Google account, which is the only user identity this
 * app has. Signed out is reported as signed out rather than as a blank, because
 * "nobody was signed in" is itself a fact about the report.
 */
async function _toastEnvironment(url: string): Promise<ToastEnvironment> {
	const shell = readShellBuild();
	return {
		machine: await _machineName(),
		user: auth.user?.email ?? 'signed out',
		client: describeToastClient({
			shellStamp: shell.kind === 'ok' ? shell.value : null,
			userAgent: typeof navigator === 'undefined' ? null : navigator.userAgent
		}),
		url
	};
}

/**
 * Copy one toast to the clipboard as a pasteable report.
 *
 * THROWS when the clipboard is unavailable, by house rule: a copy that silently
 * did nothing is worse than a visible failure, because the user only finds out
 * when they paste the wrong thing into an issue. The caller turns the throw
 * into something on screen.
 *
 * Returns the text it wrote, so a test or an agent can assert on the payload
 * without reading the system clipboard.
 */
export async function copyToast(logId: string): Promise<string> {
	const toast = _find(logId);
	if (toast === undefined) {
		throw new Error(`copyToast: no toast with id ${logId} is on screen`);
	}
	const href = typeof window === 'undefined' ? '' : (window.location?.href ?? '');
	// Query stripped: it can carry ids a report has no business republishing,
	// and the path is what identifies the surface.
	const page = href === '' ? UNKNOWN : href.split('?')[0].split('#')[0];
	const occurrenceSuffix =
		toast.count > 1 ? `\nOccurrences: ${toast.count}` : '';
	const text = buildToastReport({
		id: toast.logId,
		kind: toast.kind,
		headline: toast.headline,
		message: `${toast.message}${occurrenceSuffix}`,
		detail: toast.detail,
		clientEventId: toast.clientEventId,
		serverEventId: toast.serverEventId,
		errorId: toast.errorId,
		sentryEventId: toast.sentryEventId,
		createdAt: toast.createdAt,
		env: await _toastEnvironment(page)
	});
	await writeToastReport(
		text,
		typeof navigator === 'undefined' ? undefined : navigator.clipboard,
		typeof window !== 'undefined' && window.isSecureContext === true
	);
	return text;
}

export const health = $state<{
	data: HealthOut | null;
	bindWarning: string | null;
	lastOkAt: number | null;
	lastAttemptAt: number | null;
	lastError: string | null;
	history: HealthHistoryEntry[];
}>({
	data: null,
	bindWarning: null,
	lastOkAt: null,
	lastAttemptAt: null,
	lastError: null,
	history: []
});

export async function refreshHealth(): Promise<void> {
	const now = Date.now();
	try {
		const { health: data, bindWarning } = await getHealth();
		health.data = data;
		health.bindWarning = bindWarning;
		health.lastOkAt = now;
		health.lastAttemptAt = now;
		health.lastError = null;
		health.history = pushHistory(health.history, entryFromHealth(data, now));
	} catch (exc) {
		health.data = null;
		health.lastAttemptAt = now;
		health.lastError = exc instanceof Error ? exc.message : String(exc);
	}
}
