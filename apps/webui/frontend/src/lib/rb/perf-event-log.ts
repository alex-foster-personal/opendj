/**
 * Durable client-side ring log for performance timings + failures.
 * Survives the toast disappearing: localStorage + console + in-memory.
 * Inspect: localStorage mdt.perfEventLog or window.__mdtPerfLog().
 *
 * Load timings are always recorded (no opt-in flag). One compact
 * console.info line per load so DevTools filter `[perf]` is enough.
 */

export interface PerfEvent {
	t: string;
	kind: string;
	deck: 1 | 2 | 3 | 4 | null;
	message: string;
	/** Present for timing rows (ms per stage). */
	stages?: Record<string, number>;
}

const MAX = 40;
const STORAGE_KEY = 'mdt.perfEventLog';

let _events: PerfEvent[] = _readStorage();

function _readStorage(): PerfEvent[] {
	if (typeof localStorage === 'undefined') return [];
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (raw === null || raw === '') return [];
		const parsed: unknown = JSON.parse(raw);
		if (!Array.isArray(parsed)) return [];
		return parsed.filter(
			(e): e is PerfEvent =>
				typeof e === 'object' &&
				e !== null &&
				typeof (e as PerfEvent).t === 'string' &&
				typeof (e as PerfEvent).kind === 'string' &&
				typeof (e as PerfEvent).message === 'string'
		);
	} catch {
		return [];
	}
}

function _writeStorage(): void {
	if (typeof localStorage === 'undefined') return;
	try {
		localStorage.setItem(STORAGE_KEY, JSON.stringify(_events));
	} catch {
		/* private mode / quota - console still has the line */
	}
}

function _push(entry: PerfEvent): void {
	_events = [..._events.slice(-(MAX - 1)), entry];
	_writeStorage();
}

function _stageSummary(stages: Record<string, number>): string {
	return Object.entries(stages)
		.map(([k, v]) => `${k}=${v}`)
		.join(' ');
}

/** Append a performance failure/event for after-the-fact diagnosis. */
export function recordPerfEvent(
	kind: string,
	message: string,
	deck: 1 | 2 | 3 | 4 | null = null
): void {
	_push({
		t: new Date().toISOString(),
		kind,
		deck,
		message
	});
	const deckBit = deck === null ? '' : ` deck=${deck}`;
	console.warn(`[perf-event] ${kind}${deckBit}: ${message}`);
}

/** Always-on stage timing (ms). One console.info + ring entry. */
export function recordPerfTiming(
	kind: string,
	stages: Record<string, number>,
	deck: 1 | 2 | 3 | 4 | null = null
): void {
	const message = _stageSummary(stages);
	_push({
		t: new Date().toISOString(),
		kind,
		deck,
		message,
		stages: { ...stages }
	});
	const deckBit = deck === null ? '' : ` deck=${deck}`;
	console.info(`[perf] ${kind}${deckBit} ${message}`);
}

export function readPerfEvents(): readonly PerfEvent[] {
	return _events;
}

/** DevTools helper: copy(window.__mdtPerfLog()) after a mystery failure. */
export function installPerfEventLogGlobal(): void {
	if (typeof window === 'undefined') return;
	(
		window as Window & { __mdtPerfLog?: () => readonly PerfEvent[] }
	).__mdtPerfLog = () => readPerfEvents();
}
