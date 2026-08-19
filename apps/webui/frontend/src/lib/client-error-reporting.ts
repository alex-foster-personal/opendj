import { api } from './api/client';

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
		const queue = readQueue();
		while (queue.length > 0) {
			// Non-2xx becomes ApiError; network failures throw too. Either path
			// stops the loop without removing the head item so the durable
			// browser queue can retry on the next report or page load.
			await api.POST('/api/v1/client-errors', {
				body: queue[0],
				keepalive: true
			});
			queue.shift();
			writeQueue(queue);
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
	window.addEventListener('error', (event) => {
		reportClientError(event.error ?? event.message, { source: 'window' }, 'window-error');
	});
	window.addEventListener('unhandledrejection', (event) => {
		reportClientError(event.reason, { source: 'unhandledrejection' }, 'unhandled-rejection');
	});
	void flushQueue();
}
