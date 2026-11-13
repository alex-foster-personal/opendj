import { API_BASE } from '$lib/api/base';
import type { WebSocketLike } from '$lib/api/events-bus';

import type { FrameMessage, FixtureState, Rgb, StageFixture, StagePatch } from './stage-render';

// Served by apps/webui/server/routes/lighting.py (GET patch, POST + ws frames).
export const FRAMES_PATH = '/api/v1/lighting/frames';
export const PATCH_PATH = '/api/v1/lighting/patch';

export type FramesStatusState = 'connecting' | 'live' | 'closed' | 'error';

export interface FramesStatus {
	state: FramesStatusState;
	detail: string;
}

export interface ReplaySource {
	patch: StagePatch | null;
	frames: FrameMessage[];
	durationS: number;
	frameAt(elapsedS: number): { frame: FrameMessage; index: number };
}

function clamp01(v: number): number {
	if (v < 0) return 0;
	if (v > 1) return 1;
	return v;
}

function parseRgb(raw: unknown, path: string): Rgb {
	if (typeof raw === 'string') {
		const m = /^#([0-9a-fA-F]{6})$/.exec(raw.trim());
		if (m === null) throw new Error(`${path}: expected #rrggbb hex string`);
		const hex = m[1];
		return [
			parseInt(hex.slice(0, 2), 16),
			parseInt(hex.slice(2, 4), 16),
			parseInt(hex.slice(4, 6), 16)
		];
	}
	if (!Array.isArray(raw) || raw.length !== 3) {
		throw new Error(`${path}: expected [r,g,b] with three numbers 0..255`);
	}
	const out: [number, number, number] = [0, 0, 0];
	for (let i = 0; i < 3; i++) {
		const v = raw[i];
		if (typeof v !== 'number' || !Number.isFinite(v)) {
			throw new Error(`${path}[${i}]: expected a number`);
		}
		let c = v;
		if (c < 0) c = 0;
		if (c > 255) c = 255;
		out[i] = c;
	}
	return out;
}

function parseFixtureState(raw: unknown, index: number): FixtureState {
	const path = `fixtures[${index}]`;
	if (raw === null || typeof raw !== 'object') throw new Error(`${path}: expected object`);
	const obj = raw as Record<string, unknown>;
	const id = obj.id;
	if (typeof id !== 'string' || id.length === 0)
		throw new Error(`${path}.id: expected non-empty string`);
	const rgb = parseRgb(obj.rgb, `${path}.rgb`);
	const intensityRaw = obj.intensity;
	if (typeof intensityRaw !== 'number' || !Number.isFinite(intensityRaw)) {
		throw new Error(`${path}.intensity: expected a number`);
	}
	const panRaw = obj.pan_deg;
	const pan_deg =
		panRaw === undefined || panRaw === null
			? null
			: typeof panRaw === 'number' && Number.isFinite(panRaw)
				? panRaw
				: (() => {
						throw new Error(`${path}.pan_deg: expected number or null`);
					})();
	const tiltRaw = obj.tilt_deg;
	const tilt_deg =
		tiltRaw === undefined || tiltRaw === null
			? null
			: typeof tiltRaw === 'number' && Number.isFinite(tiltRaw)
				? tiltRaw
				: (() => {
						throw new Error(`${path}.tilt_deg: expected number or null`);
					})();
	const strobeRaw = obj.strobe_hz;
	const strobe_hz =
		strobeRaw === undefined
			? 0
			: typeof strobeRaw === 'number' && Number.isFinite(strobeRaw)
				? strobeRaw
				: (() => {
						throw new Error(`${path}.strobe_hz: expected a number`);
					})();
	return {
		id,
		rgb,
		intensity: clamp01(intensityRaw),
		pan_deg,
		tilt_deg,
		strobe_hz
	};
}

export function parseFrameMessage(raw: unknown): FrameMessage {
	if (raw === null || typeof raw !== 'object') throw new Error('frame: expected object');
	const obj = raw as Record<string, unknown>;
	const t_ns = obj.t_ns;
	if (typeof t_ns !== 'number' || !Number.isFinite(t_ns)) {
		throw new Error('t_ns: expected a finite number');
	}
	const fixturesRaw = obj.fixtures;
	if (!Array.isArray(fixturesRaw)) throw new Error('fixtures: expected array');
	const patch_version =
		obj.patch_version === undefined ? null : (obj.patch_version as string | number | null);
	const fixtures = fixturesRaw.map((item, i) => parseFixtureState(item, i));
	return { t_ns, patch_version, fixtures };
}

function readPosition(obj: Record<string, unknown>, path: string): { x: number; y: number } {
	const pos = obj.position;
	if (Array.isArray(pos) && pos.length >= 2) {
		const x = pos[0];
		const y = pos[1];
		if (typeof x === 'number' && typeof y === 'number') return { x, y };
		throw new Error(`${path}.position: expected [x, y] numbers`);
	}
	if (pos !== null && typeof pos === 'object' && !Array.isArray(pos)) {
		const po = pos as Record<string, unknown>;
		const x = po.x;
		const y = po.y;
		if (typeof x === 'number' && typeof y === 'number') return { x, y };
		throw new Error(`${path}.position: expected { x, y } numbers`);
	}
	const x = obj.x;
	const y = obj.y;
	if (typeof x === 'number' && typeof y === 'number') return { x, y };
	throw new Error(`${path}: expected x/y or position`);
}

function parseStageFixture(raw: unknown, index: number): StageFixture {
	const path = `fixtures[${index}]`;
	if (raw === null || typeof raw !== 'object') throw new Error(`${path}: expected object`);
	const obj = raw as Record<string, unknown>;
	const id = obj.id;
	if (typeof id !== 'string' || id.length === 0)
		throw new Error(`${path}.id: expected non-empty string`);
	const { x, y } = readPosition(obj, path);
	const groupsRaw = obj.groups;
	const groups = Array.isArray(groupsRaw)
		? groupsRaw.filter((g): g is string => typeof g === 'string')
		: [];
	const out: StageFixture = { id, x, y, groups };
	if (typeof obj.pan_range_deg === 'number' && Number.isFinite(obj.pan_range_deg)) {
		out.pan_range_deg = obj.pan_range_deg;
	}
	if (typeof obj.tilt_range_deg === 'number' && Number.isFinite(obj.tilt_range_deg)) {
		out.tilt_range_deg = obj.tilt_range_deg;
	}
	return out;
}

export function parsePatch(raw: unknown): StagePatch {
	if (Array.isArray(raw)) {
		return {
			version: null,
			fixtures: raw.map((item, i) => parseStageFixture(item, i))
		};
	}
	if (raw === null || typeof raw !== 'object') throw new Error('patch: expected object or array');
	const obj = raw as Record<string, unknown>;
	let fixturesRaw = obj.fixtures;
	if (fixturesRaw === undefined && Array.isArray(obj.patches)) {
		// Daemon venue store {version, active, patches}: resolve the active patch.
		const active = obj.patches.find(
			(p) =>
				p !== null && typeof p === 'object' && (p as Record<string, unknown>).name === obj.active
		);
		if (active === undefined) throw new Error('patch.active: no patch with that name');
		fixturesRaw = (active as Record<string, unknown>).fixtures;
	}
	if (!Array.isArray(fixturesRaw)) throw new Error('patch.fixtures: expected array');
	const version = obj.version === undefined ? null : (obj.version as string | number | null);
	return {
		version,
		fixtures: fixturesRaw.map((item, i) => parseStageFixture(item, i))
	};
}

export function parseReplayFile(text: string): {
	patch: StagePatch | null;
	frames: FrameMessage[];
} {
	let parsed: unknown;
	try {
		parsed = JSON.parse(text);
	} catch {
		throw new Error('replay file: JSON parse failed');
	}
	let patch: StagePatch | null = null;
	let framesRaw: unknown[];
	if (Array.isArray(parsed)) {
		framesRaw = parsed;
	} else if (parsed !== null && typeof parsed === 'object') {
		const obj = parsed as Record<string, unknown>;
		if (!Array.isArray(obj.frames))
			throw new Error('replay file: frames must be a non-empty array');
		framesRaw = obj.frames;
		if (obj.patch !== undefined) patch = parsePatch(obj.patch);
	} else {
		throw new Error('replay file: expected array or { frames, patch? }');
	}
	if (framesRaw.length === 0) throw new Error('replay file: frame list is empty');
	const frames = framesRaw.map((item, i) => parseFrameMessage(item));
	const indexed = frames.map((frame, index) => ({ frame, index }));
	indexed.sort((a, b) => {
		if (a.frame.t_ns !== b.frame.t_ns) return a.frame.t_ns - b.frame.t_ns;
		return a.index - b.index;
	});
	return { patch, frames: indexed.map((x) => x.frame) };
}

export function openReplay(text: string): ReplaySource {
	const { patch, frames } = parseReplayFile(text);
	const t0 = frames[0].t_ns;
	const tLast = frames[frames.length - 1].t_ns;
	const durationS = Math.max(0, (tLast - t0) / 1e9);

	const timesS = frames.map((f) => (f.t_ns - t0) / 1e9);

	function frameAt(elapsedS: number): { frame: FrameMessage; index: number } {
		if (frames.length === 1) return { frame: frames[0], index: 0 };
		if (elapsedS <= timesS[0]) return { frame: frames[0], index: 0 };
		const last = frames.length - 1;
		if (elapsedS >= timesS[last]) return { frame: frames[last], index: last };
		let lo = 0;
		let hi = last;
		while (lo < hi) {
			const mid = Math.ceil((lo + hi) / 2);
			if (timesS[mid] <= elapsedS) lo = mid;
			else hi = mid - 1;
		}
		return { frame: frames[lo], index: lo };
	}

	return { patch, frames, durationS, frameAt };
}

export async function fetchPatch(fetchImpl: typeof fetch = globalThis.fetch): Promise<StagePatch> {
	const url = `${API_BASE}${PATCH_PATH}`;
	const res = await fetchImpl(url);
	if (!res.ok) {
		let detail = '';
		try {
			const errBody: unknown = await res.json();
			if (errBody !== null && typeof errBody === 'object') {
				const d = (errBody as Record<string, unknown>).detail;
				if (d !== null && typeof d === 'object') {
					const code = (d as Record<string, unknown>).code;
					if (typeof code === 'string' && code.length > 0) detail = ` ${code}`;
				}
			}
		} catch {
			/* non-JSON error body */
		}
		throw new Error(`fetchPatch: HTTP ${res.status}${detail}`);
	}
	const body: unknown = await res.json();
	return parsePatch(body);
}

function resolveFramesUrl(url?: string): string {
	if (url !== undefined) return url;
	const base =
		API_BASE === ''
			? (() => {
					if (typeof globalThis.location === 'undefined') {
						throw new Error(
							'frames-client: API_BASE is empty but location is unavailable; pass options.url'
						);
					}
					return globalThis.location.origin;
				})()
			: API_BASE;
	const resolved = new URL(FRAMES_PATH, base);
	resolved.protocol = resolved.protocol === 'https:' ? 'wss:' : 'ws:';
	return resolved.toString();
}

function defaultSocketFactory(url: string): WebSocketLike {
	if (typeof globalThis.WebSocket === 'undefined') {
		throw new Error('frames-client: WebSocket unavailable; pass socketFactory');
	}
	return new globalThis.WebSocket(url) as unknown as WebSocketLike;
}

export function connectFrames(
	onFrame: (frame: FrameMessage) => void,
	onStatus: (status: FramesStatus) => void,
	options?: { url?: string; socketFactory?: (url: string) => WebSocketLike }
): () => void {
	const resolvedUrl = resolveFramesUrl(options?.url);
	const factory = options?.socketFactory ?? defaultSocketFactory;
	let socket: WebSocketLike | null = null;
	let stopped = false;
	let backoffMs = 1000;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

	function clearReconnect(): void {
		if (reconnectTimer !== null) {
			clearTimeout(reconnectTimer);
			reconnectTimer = null;
		}
	}

	function scheduleReconnect(): void {
		if (stopped) return;
		clearReconnect();
		const delay = backoffMs;
		backoffMs = Math.min(backoffMs * 2, 10_000);
		reconnectTimer = setTimeout(() => {
			reconnectTimer = null;
			openSocket();
		}, delay);
	}

	function openSocket(): void {
		if (stopped) return;
		onStatus({ state: 'connecting', detail: resolvedUrl });
		socket = factory(resolvedUrl);
		socket.onopen = () => {
			backoffMs = 1000;
			onStatus({ state: 'live', detail: 'connected' });
		};
		socket.onmessage = (event) => {
			backoffMs = 1000;
			try {
				const raw = typeof event.data === 'string' ? JSON.parse(event.data) : event.data;
				onFrame(parseFrameMessage(raw));
				onStatus({ state: 'live', detail: 'frame' });
			} catch (err) {
				const detail = err instanceof Error ? err.message : String(err);
				onStatus({ state: 'error', detail });
			}
		};
		socket.onclose = (event) => {
			const detail = event.reason || `code ${event.code}`;
			onStatus({ state: 'closed', detail });
			socket = null;
			if (!stopped) scheduleReconnect();
		};
		socket.onerror = () => {
			onStatus({ state: 'error', detail: 'socket error' });
		};
	}

	openSocket();

	return () => {
		stopped = true;
		clearReconnect();
		if (socket !== null) {
			socket.close();
			socket = null;
		}
	};
}
