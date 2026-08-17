const VISITOR_ENDPOINT = '/api/v1/client-events';

let fallbackId = 0;

type TelemetryWindow = Window & {
	__musicDjToolsVisitorTelemetryInstalled?: boolean;
};

function bounded(value: string, length: number): string {
	return value.length <= length ? value : value.slice(0, length);
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
		kind: 'page-view',
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
	void fetch(VISITOR_ENDPOINT, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(payload),
		keepalive: true
	}).catch(() => {
		// Visitor telemetry must never interfere with the application path.
	});
}
