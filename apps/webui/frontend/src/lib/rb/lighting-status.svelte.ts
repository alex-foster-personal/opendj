/**
 * Lighting status for the performance top bar (issue #13353).
 *
 * Reads the `lighting.output` flag from `GET /api/v1/flags` first: while it is off (every
 * build's launch state) the chip reads off without calling `GET /api/v1/lighting/status`,
 * whose 409 the browser would log as a console error on every poll. While it is on, the
 * status route is polled and reduced to one of three read-only chip states. No lighting
 * control lives here. Pure helpers are exported for unit tests; `fetch` is injected.
 */

import { API_BASE } from '$lib/api/base';

export const LIGHTING_STATUS_PATH = '/api/v1/lighting/status';
export const FLAGS_PATH = '/api/v1/flags';
export const LIGHTING_OUTPUT_FLAG = 'lighting.output';
export const LIGHTING_STATUS_POLL_MS = 5_000;

export type LightingChipState = 'off' | 'on' | 'no_feed';

export interface LightingChip {
	state: LightingChipState;
	label: string;
	title: string;
}

/** Map an HTTP status and parsed body to the chip state, or null when unknown. */
export function lightingChipState(httpStatus: number, body: unknown): LightingChipState | null {
	if (httpStatus === 409) return 'off';
	if (httpStatus !== 200 || !body || typeof body !== 'object') return null;
	return (body as { feed_connected?: unknown }).feed_connected === true ? 'on' : 'no_feed';
}

/** Describe a chip state as the label and tooltip the top bar shows. */
export function describeLightingChip(state: LightingChipState): LightingChip {
	switch (state) {
		case 'off':
			return {
				state,
				label: 'Lights off',
				title: 'Lighting output is disabled (lighting.output flag off)'
			};
		case 'on':
			return { state, label: 'Lights on', title: 'Lighting feed connected to the daemon' };
		default:
			return {
				state,
				label: 'Lights: no feed',
				title: 'Lighting is enabled but no feed is connected'
			};
	}
}

/** Fetch the lighting status once and return its chip state, or null on failure. */
export async function fetchLightingChipState(
	fetchFn: typeof fetch = fetch
): Promise<LightingChipState | null> {
	try {
		const res = await fetchFn(`${API_BASE}${LIGHTING_STATUS_PATH}`);
		const body = res.status === 200 ? await res.json() : null;
		return lightingChipState(res.status, body);
	} catch {
		return null;
	}
}

/** Whether a `GET /api/v1/flags` body has `lighting.output` on, or null when malformed. */
export function lightingOutputEnabled(body: unknown): boolean | null {
	const flags = (body as { flags?: unknown } | null)?.flags;
	if (!Array.isArray(flags)) return null;
	// The daemon declares every flag it gates on; an undeclared one has no lighting routes.
	const row = flags.find((f) => (f as { flag_id?: unknown })?.flag_id === LIGHTING_OUTPUT_FLAG);
	return (row as { enabled?: unknown } | undefined)?.enabled === true;
}

/** Fetch the flags once and return whether lighting output is on, or null on failure. */
export async function fetchLightingOutputEnabled(
	fetchFn: typeof fetch = fetch
): Promise<boolean | null> {
	try {
		const res = await fetchFn(`${API_BASE}${FLAGS_PATH}`);
		return res.status === 200 ? lightingOutputEnabled(await res.json()) : null;
	} catch {
		return null;
	}
}

/** One poll: the flag (read only while still unknown), then the status route only if it is on. */
export async function pollLightingChip(
	outputEnabled: boolean | null,
	fetchFn: typeof fetch = fetch
): Promise<{ outputEnabled: boolean | null; state: LightingChipState | null }> {
	const enabled = outputEnabled ?? (await fetchLightingOutputEnabled(fetchFn));
	if (enabled === null) return { outputEnabled: null, state: null };
	if (!enabled) return { outputEnabled: false, state: 'off' };
	return { outputEnabled: true, state: await fetchLightingChipState(fetchFn) };
}

class LightingStatusStore {
	state = $state<LightingChipState | null>(null);
	private timer: ReturnType<typeof setTimeout> | null = null;
	private users = 0;
	// Flags resolve once at engine boot, so one good read lasts the polling session.
	private outputEnabled: boolean | null = null;

	/** Start polling; returns a stop function. */
	start(fetchFn: typeof fetch = fetch): () => void {
		this.users += 1;
		if (this.users === 1) {
			const tick = async () => {
				const poll = await pollLightingChip(this.outputEnabled, fetchFn);
				this.outputEnabled = poll.outputEnabled;
				this.state = poll.state;
				if (this.users > 0) this.timer = setTimeout(tick, LIGHTING_STATUS_POLL_MS);
			};
			void tick();
		}
		return () => {
			this.users = Math.max(0, this.users - 1);
			if (this.users === 0 && this.timer) {
				clearTimeout(this.timer);
				this.timer = null;
				this.outputEnabled = null;
			}
		};
	}
}

export const lightingStatus = new LightingStatusStore();
