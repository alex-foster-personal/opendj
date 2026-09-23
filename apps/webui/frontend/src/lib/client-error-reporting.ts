import { api } from './api/client';
import { type PerfEvent, setPerfEventEscalator } from './rb/perf-event-log';

const QUEUE_KEY = 'music-dj-tools:client-errors:v1';
const MAX_QUEUE = 20;
const DEDUPE_MS = 10_000;
const CONSOLE_ERROR_SAMPLE_CUTOFF = 0.25;
const CONSOLE_WARN_SAMPLE_CUTOFF = 0.1;

type ContextValue = string | number | boolean | null;
export type ClientErrorContext = Record<string, ContextValue>;

export type ClientErrorKind =
	| 'window-error'
	| 'unhandled-rejection'
	| 'sveltekit'
	| 'ui-error'
	| 'console-error'
	| 'console-warn'
	| 'resource-error'
	| 'csp-violation'
	| 'webview-console'
	| 'webview-navigation';

interface ClientErrorPayload {
	client_event_id: string;
	kind: ClientErrorKind;
	message: string;
	name: string | null;
	stack: string | null;
	url: string;
	client_timestamp: string;
	user_agent: string;
	secure_context: boolean;
	audio_worklet_available: boolean;
	/**
	 * Was any deck playing or audible when this error fired? The engine holds
	 * the Sentry forward (never the local log) while this is true, which is
	 * the "never send while a deck is live" rule of the error-reporting
	 * policy. `null` means no transport probe is registered yet (the audio
	 * engine has not booted), so the engine falls back to its own UI-mirror
	 * read rather than trusting a default.
	 */
	any_deck_live: boolean | null;
	context: ClientErrorContext;
}

/**
 * The page's cheapest "is the set live" read, registered by app-init once the
 * audio engine module is loaded. Not imported here: this module runs from
 * hooks.client.ts on every boot, and pulling audio-engine.svelte.ts into
 * that path would load the whole graph before the first paint.
 */
let liveTransportProbe: (() => boolean) | null = null;

export function setLiveTransportProbe(probe: (() => boolean) | null): void {
	liveTransportProbe = probe;
}

function readAnyDeckLive(): boolean | null {
	if (liveTransportProbe === null) return null;
	try {
		return liveTransportProbe() === true;
	} catch {
		// A probe that throws is unknown, not "not live": let the engine fall
		// back to its mirror rather than send on the strength of a failure.
		return null;
	}
}

interface PendingShellError {
	kind: ClientErrorKind;
	message: string;
	context?: ClientErrorContext;
}

declare global {
	interface Window {
		__OPENDJ_PENDING_SHELL_ERRORS__?: PendingShellError[];
	}
}

const recent = new Map<string, number>();
let installed = false;
let flushing = false;
let fallbackId = 0;
let interceptingConsole = false;
let consoleSampleGate: () => number = () => Math.random();

export function __resetClientErrorReportingForTests(): void {
	installed = false;
}

export function __setConsoleSampleGateForTests(gate?: () => number): void {
	consoleSampleGate = gate ?? (() => Math.random());
}

function truncate(value: string, length: number): string {
	return value.length <= length ? value : value.slice(0, length);
}

function describe(cause: unknown): { message: string; name: string | null; stack: string | null } {
	if (cause instanceof Error) {
		return {
			message: truncate(cause.message || String(cause), 4096),
			name: truncate(cause.name || 'Error', 256),
			stack: cause.stack ? truncate(cause.stack, 32768) : null
		};
	}
	if (typeof cause === 'string') return { message: truncate(cause, 4096), name: null, stack: null };
	try {
		return { message: truncate(JSON.stringify(cause), 4096), name: null, stack: null };
	} catch {
		return { message: truncate(String(cause), 4096), name: null, stack: null };
	}
}

function readQueue(): ClientErrorPayload[] {
	try {
		const raw = localStorage.getItem(QUEUE_KEY);
		if (raw === null) return [];
		const parsed = JSON.parse(raw);
		return Array.isArray(parsed) ? (parsed as ClientErrorPayload[]).slice(-MAX_QUEUE) : [];
	} catch {
		return [];
	}
}

function writeQueue(queue: ClientErrorPayload[]): void {
	try {
		localStorage.setItem(QUEUE_KEY, JSON.stringify(queue.slice(-MAX_QUEUE)));
	} catch {
		// Reporting must never replace the original application error.
	}
}

async function flushQueue(): Promise<void> {
	if (flushing || typeof window === 'undefined') return;
	flushing = true;
	try {
		for (;;) {
			const queue = readQueue();
			if (queue.length === 0) break;
			const sent = queue[0];
			await api.POST('/api/v1/client-errors', {
				body: sent,
				keepalive: true
			});
			const remaining = readQueue();
			const at = remaining.findIndex((row) => row.client_event_id === sent.client_event_id);
			if (at >= 0) remaining.splice(at, 1);
			writeQueue(remaining);
		}
	} catch {
		// The durable browser queue retries on the next report or page load.
	} finally {
		flushing = false;
	}
}

function shouldReport(kind: ClientErrorKind, fingerprint: string): boolean {
	const now = Date.now();
	if (now - (recent.get(fingerprint) ?? 0) < DEDUPE_MS) return false;
	recent.set(fingerprint, now);
	return true;
}

function formatConsoleArgs(args: unknown[]): string {
	const parts = args.map((value) => {
		if (typeof value === 'string') return value;
		if (value instanceof Error) return value.stack ?? value.message;
		try {
			return JSON.stringify(value);
		} catch {
			return String(value);
		}
	});
	return truncate(parts.join(' '), 4096);
}

function consolePassesSample(kind: 'console-error' | 'console-warn'): boolean {
	const cutoff =
		kind === 'console-error' ? CONSOLE_ERROR_SAMPLE_CUTOFF : CONSOLE_WARN_SAMPLE_CUTOFF;
	return consoleSampleGate() < cutoff;
}

function patchConsole(kind: 'console-error' | 'console-warn'): void {
	const original = kind === 'console-error' ? console.error.bind(console) : console.warn.bind(console);
	const forward = (...args: unknown[]) => {
		original(...args);
		if (interceptingConsole) return;
		const message = formatConsoleArgs(args);
		if (!consolePassesSample(kind)) return;
		interceptingConsole = true;
		try {
			reportClientError(message, { source: kind, console_level: kind }, kind);
		} finally {
			interceptingConsole = false;
		}
	};
	if (kind === 'console-error') {
		console.error = forward as typeof console.error;
	} else {
		console.warn = forward as typeof console.warn;
	}
}

function resourceTarget(event: Event): EventTarget | null {
	const target = event.target;
	if (target === null || target === undefined) return null;
	if (target === window) return null;
	if (typeof Element !== 'undefined') {
		if (target instanceof Element) {
			if (target === document.documentElement || target === document.body) return null;
			return target;
		}
		return null;
	}
	if (typeof target === 'object' && 'tagName' in target) {
		return target;
	}
	return null;
}

function resourceSource(target: EventTarget): string | null {
	if (typeof Element !== 'undefined' && target instanceof Element) {
		if (target instanceof HTMLScriptElement) return target.src || null;
		if (target instanceof HTMLLinkElement) return target.href || null;
		if (target instanceof HTMLImageElement) return target.currentSrc || target.src || null;
		if (target instanceof HTMLMediaElement) return target.currentSrc || target.src || null;
	}
	const named = target as EventTarget & { tagName?: string; src?: string; href?: string };
	return named.src ?? named.href ?? null;
}

function handleResourceError(event: Event): void {
	const target = resourceTarget(event);
	if (target === null) return;
	const source = resourceSource(target) ?? 'unknown';
	const tagName =
		typeof Element !== 'undefined' && target instanceof Element
			? target.tagName
			: String((target as { tagName?: string }).tagName ?? 'unknown');
	const message = truncate(`Failed to load ${tagName.toLowerCase()}: ${source}`, 4096);
	reportClientError(
		message,
		{
			source: 'resource-error',
			tag: tagName,
			resource: source
		},
		'resource-error'
	);
}

function handleCspViolation(event: Event): void {
	if (!('violatedDirective' in event)) return;
	const violation = event as SecurityPolicyViolationEvent;
	const message = truncate(
		`${violation.violatedDirective}: ${violation.blockedURI || '(inline)'}`,
		4096
	);
	reportClientError(
		message,
		{
			source: 'csp-violation',
			violated_directive: violation.violatedDirective,
			effective_directive: violation.effectiveDirective,
			blocked_uri: violation.blockedURI,
			document_uri: violation.documentURI
		},
		'csp-violation'
	);
}

function drainPendingShellErrors(): void {
	if (typeof window === 'undefined') return;
	const pending = window.__OPENDJ_PENDING_SHELL_ERRORS__;
	if (!Array.isArray(pending) || pending.length === 0) return;
	for (const row of pending.splice(0, pending.length)) {
		reportClientError(row.message, row.context ?? { source: 'shell-webview' }, row.kind);
	}
}

export function reportClientError(
	cause: unknown,
	context: ClientErrorContext = {},
	kind: ClientErrorKind = 'ui-error'
): void {
	if (typeof window === 'undefined') return;
	const described = describe(cause);
	const fingerprint = `${kind}:${context.source ?? ''}:${described.message}`;
	if (!shouldReport(kind, fingerprint)) return;
	const now = Date.now();
	const clientEventId =
		typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
			? crypto.randomUUID()
			: `client-${now}-${++fallbackId}`;
	const payload: ClientErrorPayload = {
		client_event_id: clientEventId,
		kind,
		...described,
		url: truncate(window.location?.href ?? '', 4096),
		client_timestamp: new Date(now).toISOString(),
		user_agent: truncate(typeof navigator === 'undefined' ? '' : navigator.userAgent, 2048),
		secure_context: window.isSecureContext === true,
		audio_worklet_available: typeof AudioWorkletNode !== 'undefined',
		any_deck_live: readAnyDeckLive(),
		context: Object.fromEntries(
			Object.entries(context)
				.slice(0, 32)
				.map(([key, value]) => [truncate(key, 128), typeof value === 'string' ? truncate(value, 4096) : value])
		)
	};
	const queue = readQueue();
	queue.push(payload);
	writeQueue(queue);
	void flushQueue();
}

export function installClientErrorReporting(): void {
	if (installed || typeof window === 'undefined') return;
	installed = true;
	setPerfEventEscalator((event: PerfEvent) =>
		reportClientError(
			`${event.kind}: ${event.message}`,
			{
				source: 'perf-event',
				perf_kind: event.kind,
				deck: event.deck,
				...(event.id === undefined ? {} : { perf_event_id: event.id })
			},
			'ui-error'
		)
	);
	window.addEventListener('error', (event) => {
		reportClientError(event.error ?? event.message, { source: 'window' }, 'window-error');
	});
	window.addEventListener('error', handleResourceError, true);
	window.addEventListener('securitypolicyviolation', handleCspViolation);
	window.addEventListener('unhandledrejection', (event) => {
		reportClientError(event.reason, { source: 'unhandledrejection' }, 'unhandled-rejection');
	});
	patchConsole('console-error');
	patchConsole('console-warn');
	drainPendingShellErrors();
	void flushQueue();
}
