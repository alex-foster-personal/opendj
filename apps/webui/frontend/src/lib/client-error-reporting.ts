import { api } from './api/client';
import { type PerfEvent, setPerfEventEscalator } from './rb/perf-event-log';

const QUEUE_KEY = 'music-dj-tools:client-errors:v1';
const MAX_QUEUE = 20;
const DEDUPE_MS = 10_000;

type ContextValue = string | number | boolean | null;
export type ClientErrorContext = Record<string, ContextValue>;

interface ClientErrorPayload {
	client_event_id: string;
	kind: 'window-error' | 'unhandled-rejection' | 'sveltekit' | 'ui-error';
	message: string;
	name: string | null;
	stack: string | null;
	url: string;
	client_timestamp: string;
	user_agent: string;
	secure_context: boolean;
	audio_worklet_available: boolean;
	context: ClientErrorContext;
}

const recent = new Map<string, number>();
let installed = false;
let flushing = false;
let fallbackId = 0;

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
		// RE-READ EVERY ITERATION, AND REMOVE BY ID.
		//
		// The previous loop took ONE snapshot before the first await and wrote
		// that snapshot back after each POST. `reportClientError` is
		// synchronous and appends straight to storage, so a report raised while
		// a POST was in flight landed in storage and was then ERASED by the
		// stale snapshot being written over it. Two reports of one failure in
		// the same tick is the ordinary case (a toast reports, and something it
		// called reports), so this was not a rare interleaving.
		//
		// Removing the sent row by `client_event_id` rather than shifting a
		// position also survives a queue that was trimmed to MAX_QUEUE
		// underneath us, where index 0 is no longer the row that was sent.
		for (;;) {
			const queue = readQueue();
			if (queue.length === 0) break;
			const sent = queue[0];
			// Non-2xx becomes ApiError; network failures throw too. Either path
			// leaves the head item in storage so the durable browser queue can
			// retry on the next report or page load.
			await api.POST('/api/v1/client-errors', {
				body: sent,
				keepalive: true
			});
			// The FIRST match, spliced, not every match filtered out: two rows can
			// legitimately carry the same id (the `client-<now>-<n>` fallback in a
			// context without crypto.randomUUID), and dropping both would lose an
			// unsent report to make bookkeeping tidier.
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

export function reportClientError(
	cause: unknown,
	context: ClientErrorContext = {},
	kind: ClientErrorPayload['kind'] = 'ui-error'
): void {
	if (typeof window === 'undefined') return;
	const described = describe(cause);
	const fingerprint = `${kind}:${context.source ?? ''}:${described.message}`;
	const now = Date.now();
	if (now - (recent.get(fingerprint) ?? 0) < DEDUPE_MS) return;
	recent.set(fingerprint, now);
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
	// Point the perf ring's escalated rows here. The dependency runs THIS way
	// round on purpose: perf-event-log must stay import-free, because it is
	// reached transitively by Playwright specs loaded under plain Node, where
	// `$lib/api/client.ts` evaluating import.meta.env at module scope kills the
	// whole config at load time. See setPerfEventEscalator's comment.
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
	window.addEventListener('unhandledrejection', (event) => {
		reportClientError(event.reason, { source: 'unhandledrejection' }, 'unhandled-rejection');
	});
	void flushQueue();
}
