/**
 * Phone remote listener (issue #10441, REMOTE-CTL): pure helpers for the
 * Audio I/O panel. Mirrors the engine routes from issue #10434
 * (/api/v1/performance/remote-control/...). QR entry is issue #10442.
 */

export const REMOTE_CONTROL_ROUTES = '/api/v1/performance/remote-control';
export const REMOTE_CONTROL_POLL_MS = 1_000;

export type RemoteControlStatusValue = 'off' | 'listening' | 'locked_out';
/** Alias used by the QR entry helpers (#10442). */
export type RemoteControlStatus = RemoteControlStatusValue;

export interface RemoteControlDevice {
	device_id: string;
	device_name: string;
	paired_at: string;
	last_seen: string | null;
}

export interface RemoteControlState {
	status: RemoteControlStatusValue;
	code: string | null;
	address: string | null;
	address_error: string | null;
	port: number | null;
	devices: RemoteControlDevice[];
	wrong_codes: number;
	max_wrong_codes: number;
	last_error?: string | null;
	/** True when the engine reopened the listener at boot (#10713). */
	reopened_at_boot?: boolean;
}

export type RemoteControlAction = 'status' | 'start' | 'stop' | 'rotate' | 'forget';

export function remoteControlRequest(
	action: RemoteControlAction,
	deviceId?: string
): { method: 'GET' | 'POST' | 'DELETE'; path: string; body?: Record<string, never> } {
	switch (action) {
		case 'status':
			return { method: 'GET', path: '' };
		case 'start':
			return { method: 'POST', path: '/start', body: {} };
		case 'stop':
			return { method: 'POST', path: '/stop', body: {} };
		case 'rotate':
			return { method: 'POST', path: '/rotate-code', body: {} };
		case 'forget': {
			if (deviceId === undefined || deviceId === '') {
				throw new Error('forget requires a non-empty device_id');
			}
			return { method: 'DELETE', path: `/devices/${encodeURIComponent(deviceId)}` };
		}
		default: {
			const _exhaustive: never = action;
			throw new Error(`unknown remote control action ${_exhaustive}`);
		}
	}
}

export function remoteControlUrl(state: RemoteControlState): string | null {
	if (state.address === null || state.port === null) return null;
	return `http://${state.address}:${state.port}/remote`;
}

export function remoteStatusLine(state: RemoteControlState): string {
	switch (state.status) {
		case 'off':
			return 'Phone remote is off.';
		case 'listening':
			if (state.address !== null && state.port !== null) {
				return `Listening on ${state.address}:${state.port}. Wrong codes: ${state.wrong_codes} of ${state.max_wrong_codes}.`;
			}
			return `Listening on port ${state.port ?? '?'}, but no LAN address: ${state.address_error ?? 'unknown reason'}.`;
		case 'locked_out':
			return `Locked after ${state.max_wrong_codes} wrong codes. Start again to issue a new code.`;
		default: {
			const _exhaustive: never = state.status;
			throw new Error(`unknown remote control status ${_exhaustive}`);
		}
	}
}

const UTC_MONTHS = [
	'Jan',
	'Feb',
	'Mar',
	'Apr',
	'May',
	'Jun',
	'Jul',
	'Aug',
	'Sep',
	'Oct',
	'Nov',
	'Dec'
] as const;
const UTC_DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'] as const;

export function relativeSeen(iso: string | null, nowMs = Date.now()): string {
	if (iso === null) return 'never seen';
	const at = Date.parse(iso);
	if (Number.isNaN(at)) return 'never seen';
	const ageMs = nowMs - at;
	if (ageMs < 60_000) return 'just now';
	const minutes = Math.floor(ageMs / 60_000);
	if (minutes < 60) return `${minutes} min ago`;
	const hours = Math.floor(ageMs / 3_600_000);
	if (hours < 24) return `${hours} h ago`;
	const days = Math.floor(ageMs / 86_400_000);
	return `${days} d ago`;
}

export function pairedDate(iso: string): string {
	const d = new Date(iso);
	if (Number.isNaN(d.getTime())) return 'unknown date';
	return `${UTC_DAYS[d.getUTCDay()]} ${d.getUTCDate()} ${UTC_MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

export interface PollTimers {
	setInterval(fn: () => void, ms: number): unknown;
	clearInterval(handle: unknown): void;
}

const defaultTimers: PollTimers = {
	setInterval: (fn, ms) => globalThis.setInterval(fn, ms),
	clearInterval: (handle) => {
		globalThis.clearInterval(handle as ReturnType<typeof setInterval>);
	}
};

export function startStatusPoll(
	refresh: () => void | Promise<void>,
	timers: PollTimers = defaultTimers,
	intervalMs = REMOTE_CONTROL_POLL_MS
): () => void {
	let stopped = false;
	let pending = false;
	const tick = () => {
		if (stopped || pending) return;
		pending = true;
		void Promise.resolve(refresh()).finally(() => {
			pending = false;
		});
	};
	const handle = timers.setInterval(tick, intervalMs);
	return () => {
		if (stopped) return;
		stopped = true;
		timers.clearInterval(handle);
	};
}

export type QrTarget = 'remote' | 'request' | 'display';

/** Enabled QR targets on the panel. Request/display join once those products land. */
export const ENABLED_QR_TARGETS: readonly QrTarget[] = Object.freeze(['remote']);

export function remoteControlStatusLine(state: RemoteControlState): string {
	if (state.status === 'off') {
		return state.last_error ? `Off (${state.last_error})` : 'Off';
	}
	if (state.status === 'locked_out') {
		return `Locked after ${state.max_wrong_codes} wrong codes. Start again to issue a new code.`;
	}
	const where =
		state.address && state.port !== null
			? `http://${state.address}:${state.port}/remote`
			: 'address pending';
	const boot = state.reopened_at_boot ? ' (reopened at boot)' : '';
	return `Listening ${where}${boot}`;
}

/** Cache-busting QR image URL: changes when the pairing code rotates. */
export function remoteQrImageUrl(target: QrTarget, code: string | null): string {
	const bust = code && code.length > 0 ? code : 'off';
	return `${REMOTE_CONTROL_ROUTES}/qr.svg?target=${target}&v=${encodeURIComponent(bust)}`;
}
