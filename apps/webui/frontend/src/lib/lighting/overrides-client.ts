/**
 * Lighting overrides client (issue #9899, spec item L56).
 *
 * One typed wrapper over `GET`/`POST /api/v1/lighting/overrides` (the endpoint the CLI
 * `opendj lighting override` and the MCP tool drive too), a strict response parser, a
 * hold-to-confirm gate for blackout and a chained poller so the panel reflects server truth.
 * `fetch` and the clock are injected so unit tests drive everything without a browser or
 * backend. Keep this file free of `$lib`, svelte and DOM imports.
 */

export const OVERRIDE_NAMES = [
	'blackout',
	'dimmer',
	'colour',
	'strobe',
	'mood',
	'ambient'
] as const;
export const MOODS = ['auto', 'high', 'mid', 'low'] as const;
export const STROBE_SPEEDS = ['auto', 'off', 'slow', 'mid', 'fast'] as const;

export type OverrideName = (typeof OVERRIDE_NAMES)[number];
export type Mood = (typeof MOODS)[number];
export type StrobeSpeed = (typeof STROBE_SPEEDS)[number];
export type OverrideColour = 'auto' | `#${string}`;

export interface LightingOverrides {
	blackout: boolean;
	dimmer: number;
	colour: OverrideColour;
	strobe: StrobeSpeed;
	mood: Mood;
	ambient: boolean;
}
export type OverridesPatch = Partial<LightingOverrides>;

export const OVERRIDES_PATH = '/api/v1/lighting/overrides';
export const BLACKOUT_HOLD_MS = 300;
export const OVERRIDES_POLL_MS = 500;

export type FetchLike = (
	url: string,
	init?: { method?: string; headers?: Record<string, string>; body?: string }
) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;

export interface Clock {
	setTimeout(fn: () => void, ms: number): unknown;
	clearTimeout(handle: unknown): void;
}

export class LightingOverridesHttpError extends Error {
	status: number;
	detail: unknown;
	constructor(method: string, status: number, detail: unknown) {
		const suffix = detail === null ? '' : ` ${JSON.stringify(detail)}`;
		super(`Lighting overrides ${method} failed: HTTP ${status}${suffix}`);
		this.name = 'LightingOverridesHttpError';
		this.status = status;
		this.detail = detail;
	}
}

//----- parsing

const HEX_COLOUR = /^#[0-9a-f]{6}$/;

export function parseOverrides(body: unknown): LightingOverrides {
	if (typeof body !== 'object' || body === null)
		throw new TypeError('overrides: body is not an object');
	const b = body as Record<string, unknown>;
	for (const name of OVERRIDE_NAMES) {
		if (!(name in b)) throw new TypeError(`overrides.${name}: missing`);
	}
	if (typeof b.blackout !== 'boolean') throw new TypeError('overrides.blackout: not a boolean');
	if (typeof b.dimmer !== 'number' || !(b.dimmer >= 0 && b.dimmer <= 1))
		throw new TypeError(`overrides.dimmer: ${JSON.stringify(b.dimmer)} is not a number in 0..1`);
	if (typeof b.colour !== 'string' || (b.colour !== 'auto' && !HEX_COLOUR.test(b.colour)))
		throw new TypeError(`overrides.colour: ${JSON.stringify(b.colour)} is not 'auto' or '#rrggbb'`);
	if (!(STROBE_SPEEDS as readonly unknown[]).includes(b.strobe))
		throw new TypeError(
			`overrides.strobe: ${JSON.stringify(b.strobe)} is not one of ${STROBE_SPEEDS.join(', ')}`
		);
	if (!(MOODS as readonly unknown[]).includes(b.mood))
		throw new TypeError(
			`overrides.mood: ${JSON.stringify(b.mood)} is not one of ${MOODS.join(', ')}`
		);
	if (typeof b.ambient !== 'boolean') throw new TypeError('overrides.ambient: not a boolean');
	return {
		blackout: b.blackout,
		dimmer: b.dimmer,
		colour: b.colour as OverrideColour,
		strobe: b.strobe as StrobeSpeed,
		mood: b.mood as Mood,
		ambient: b.ambient
	};
}

//----- http

async function _readDetail(res: { json(): Promise<unknown> }): Promise<unknown> {
	try {
		const body = await res.json();
		return typeof body === 'object' && body !== null && 'detail' in body
			? (body as { detail: unknown }).detail
			: null;
	} catch {
		return null; // non-JSON error body: the status alone is reported
	}
}

async function _request(fetchFn: FetchLike, url: string, method: 'GET' | 'POST', body?: string) {
	const init =
		body === undefined
			? { method }
			: { method, headers: { 'content-type': 'application/json' }, body };
	const res = await fetchFn(url, init);
	if (!res.ok) throw new LightingOverridesHttpError(method, res.status, await _readDetail(res));
	return parseOverrides(await res.json());
}

export function getOverrides(fetchFn: FetchLike, baseUrl: string): Promise<LightingOverrides> {
	return _request(fetchFn, `${baseUrl}${OVERRIDES_PATH}`, 'GET');
}

export async function postOverrides(
	fetchFn: FetchLike,
	baseUrl: string,
	patch: OverridesPatch
): Promise<LightingOverrides> {
	const keys = Object.keys(patch);
	if (keys.length === 0) throw new Error('postOverrides: empty patch');
	const unknown = keys.filter((k) => !(OVERRIDE_NAMES as readonly string[]).includes(k));
	if (unknown.length) throw new Error(`postOverrides: unknown override(s) ${unknown.join(', ')}`);
	return _request(fetchFn, `${baseUrl}${OVERRIDES_PATH}`, 'POST', JSON.stringify(patch));
}

//----- hold gate

export interface HoldGate {
	press(): void;
	release(): void;
	cancel(): void;
	readonly holding: boolean;
}

/** Confirms once when held for `holdMs`; a release or cancel before then confirms nothing. */
export function createHoldGate(opts: {
	holdMs: number;
	clock: Clock;
	onConfirm: () => void;
}): HoldGate {
	let timer: unknown = null;
	let pressed = false;
	const clear = () => {
		if (timer !== null) opts.clock.clearTimeout(timer);
		timer = null;
		pressed = false;
	};
	return {
		press() {
			if (pressed) return;
			pressed = true;
			timer = opts.clock.setTimeout(() => {
				timer = null;
				opts.onConfirm();
			}, opts.holdMs);
		},
		release: clear,
		cancel: clear,
		get holding() {
			return timer !== null;
		}
	};
}

//----- poller

export interface OverridesPoller {
	start(): void;
	stop(): void;
	refresh(): Promise<void>;
}

/** GETs now, then again `intervalMs` after each GET settles; every failure reaches `onError`. */
export function createOverridesPoller(opts: {
	fetchFn: FetchLike;
	baseUrl: string;
	intervalMs: number;
	clock: Clock;
	onState: (state: LightingOverrides) => void;
	onError: (err: unknown) => void;
}): OverridesPoller {
	let running = false;
	let timer: unknown = null;
	const refresh = async () => {
		try {
			const state = await getOverrides(opts.fetchFn, opts.baseUrl);
			if (running) opts.onState(state);
		} catch (err) {
			if (running) opts.onError(err);
		}
	};
	const tick = async () => {
		timer = null;
		await refresh();
		if (running) timer = opts.clock.setTimeout(tick, opts.intervalMs);
	};
	return {
		start() {
			if (running) return;
			running = true;
			void tick();
		},
		stop() {
			running = false;
			if (timer !== null) opts.clock.clearTimeout(timer);
			timer = null;
		},
		refresh
	};
}
