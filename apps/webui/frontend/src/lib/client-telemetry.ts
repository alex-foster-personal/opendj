import { api } from './api/client';

let fallbackId = 0;

const LOGIN_SUBMIT_KEY = 'mdt.loginSubmitAt';
const PERF_SPAN_LOGIN = 'login-submit-to-library-usable';
export const PERF_SPAN_OPEN_TO_LIBRARY_ROWS = 'open-to-library-rows';

type TelemetryWindow = Window & {
	__musicDjToolsVisitorTelemetryInstalled?: boolean;
};

interface LoginSubmitMark {
	t0: number;
	tNavigate?: number;
}

function bounded(value: string, length: number): string {
	return value.length <= length ? value : value.slice(0, length);
}

function navigationStartMs(): number {
	if (typeof performance === 'undefined') return Date.now();
	if (typeof performance.timeOrigin === 'number') {
		return performance.timeOrigin;
	}
	const timing = performance.timing;
	if (timing && typeof timing.navigationStart === 'number') {
		return timing.navigationStart;
	}
	return Date.now();
}

function readSubmitMark(): LoginSubmitMark | null {
	if (typeof sessionStorage === 'undefined') return null;
	const raw = sessionStorage.getItem(LOGIN_SUBMIT_KEY);
	if (raw === null) return null;
	try {
		const parsed = JSON.parse(raw) as LoginSubmitMark;
		if (typeof parsed.t0 !== 'number') return null;
		return parsed;
	} catch {
		return null;
	}
}

function writeSubmitMark(mark: LoginSubmitMark): void {
	if (typeof sessionStorage === 'undefined') return;
	sessionStorage.setItem(LOGIN_SUBMIT_KEY, JSON.stringify(mark));
}

function clearSubmitMark(): void {
	if (typeof sessionStorage === 'undefined') return;
	sessionStorage.removeItem(LOGIN_SUBMIT_KEY);
}

export function telemetryUrl(location: Location): string {
	return bounded(`${location.origin}${location.pathname}`, 4096);
}

export function telemetryReferrer(value: string): string | null {
	if (value === '') return null;
	try {
		const parsed = new URL(value);
		return bounded(`${parsed.origin}${parsed.pathname}`, 4096);
	} catch {
		return null;
	}
}

export function markLoginSubmit(now: number = Date.now()): void {
	writeSubmitMark({ t0: now });
}

export function markLoginNavigate(now: number = Date.now()): void {
	const mark = readSubmitMark();
	if (mark === null) return;
	writeSubmitMark({ ...mark, tNavigate: now });
}

export async function recordPerfSpan(args: {
	name: string;
	duration_ms: number;
	method: string;
	stages?: Record<string, number>;
}): Promise<void> {
	const now = Date.now();
	const clientEventId =
		typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
			? crypto.randomUUID()
			: `perf-${now}-${++fallbackId}`;
	const payload = {
		client_event_id: clientEventId,
		kind: 'perf-span' as const,
		name: args.name,
		duration_ms: args.duration_ms,
		method: args.method,
		stages: args.stages ?? null,
		client_timestamp: new Date(now).toISOString()
	};
	void api
		.POST('/api/v1/client-events', {
			body: payload,
			keepalive: true
		})
		.catch(() => {
			// Perf telemetry must never interfere with the application path.
		});
}

let openToLibraryRowsRecorded = false;

export function recordOpenToLibraryRows(args: {
	source: 'all-tracks' | 'playlist';
	now?: number;
}): void {
	if (openToLibraryRowsRecorded) return;
	openToLibraryRowsRecorded = true;
	const now = args.now ?? Date.now();
	const navStart = navigationStartMs();
	const durationMs = now - navStart;
	void recordPerfSpan({
		name: PERF_SPAN_OPEN_TO_LIBRARY_ROWS,
		duration_ms: durationMs,
		method: 'navigationStart to first track row first paint',
		stages: {
			boot_source: args.source === 'all-tracks' ? 1 : 2
		}
	});
}

/** Test-only reset for idempotent open-to-library-rows recording. */
export function resetOpenToLibraryRowsRecordedForTests(): void {
	openToLibraryRowsRecorded = false;
}

export function completeLibraryUsable(args: {
	source: 'all-tracks' | 'playlist';
	now?: number;
}): void {
	const mark = readSubmitMark();
	if (mark === null || mark.tNavigate === undefined) return;
	const now = args.now ?? Date.now();
	const navStart = navigationStartMs();
	const inAppMs = mark.tNavigate - mark.t0 + (now - navStart);
	const fullWallMs = now - mark.t0;
	void recordPerfSpan({
		name: PERF_SPAN_LOGIN,
		duration_ms: inAppMs,
		method:
			'client-telemetry markLoginSubmit/markLoginNavigate to recordLibraryLoadTiming (in-app, Google excluded)',
		stages: {
			pre_navigate_ms: mark.tNavigate - mark.t0,
			post_navigate_ms: now - navStart,
			full_wall_ms: fullWallMs,
			library_source: args.source === 'all-tracks' ? 1 : 2
		}
	});
	clearSubmitMark();
}

export function installVisitorTelemetry(): void {
	if (typeof window === 'undefined') return;
	const runtimeWindow = window as TelemetryWindow;
	if (runtimeWindow.__musicDjToolsVisitorTelemetryInstalled === true) return;
	runtimeWindow.__musicDjToolsVisitorTelemetryInstalled = true;
	const now = Date.now();
	const clientEventId =
		typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
			? crypto.randomUUID()
			: `visitor-${now}-${++fallbackId}`;
	const payload = {
		client_event_id: clientEventId,
		kind: 'page-view' as const,
		url: telemetryUrl(window.location),
		path: bounded(window.location.pathname || '/', 2048),
		referrer: telemetryReferrer(document.referrer),
		client_timestamp: new Date(now).toISOString(),
		user_agent: bounded(navigator.userAgent, 2048),
		language: bounded(navigator.language ?? '', 128) || null,
		secure_context: window.isSecureContext === true,
		viewport_width: Math.max(0, Math.round(window.innerWidth)),
		viewport_height: Math.max(0, Math.round(window.innerHeight))
	};
	void api
		.POST('/api/v1/client-events', {
			body: payload,
			keepalive: true
		})
		.catch(() => {
			// Visitor telemetry must never interfere with the application path.
		});
}
