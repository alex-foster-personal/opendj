/**
 * Pin 49f9d217: the compact UI config snapshot a comment pin carries.
 *
 * Mini-PRD
 *   ✔︎ ✅ 🎯 The snapshot names the route, the app mode, the engine mode, the
 *     perf tier and a fixed list of boolean feature switches.
 *     [if] an agent reading a pin cannot tell which engine was playing [then ⛔️] broken
 *   ✔︎ ✅ 🎯 Nothing else is copied: an allowlist, never a spread of prefs.
 *     [if] a playlist name, a path or a token reaches the snapshot [then ⛔️] broken
 *   ✔︎ ✅ 🎯 Query string and fragment are cut from the route.
 *     [if] `?token=` survives into the pin store [then ⛔️] broken
 *
 * The daemon enforces the same shape (PinUiConfig in
 * apps/webui/server/routes/feedback.py): slugs, booleans, no unknown keys.
 */

import type { components } from '../api-types';

export type PinUiConfig = components['schemas']['PinUiConfig'];

/** Boolean prefs worth knowing when reproducing a pinned defect. Order is the
 * order they are stored in. Add a name here to snapshot it; it must be a
 * boolean pref, and the daemon caps the list at 24. */
export const PIN_UI_CONFIG_SWITCHES = [
	'show_stems',
	'beat_sync_max',
	'auto_play_enabled',
	'next_only_filter',
	'remixes_filter',
	'vocals_filter',
	'available_offline_filter',
	'hide_broken_links',
	'jog_radial_waveform',
	'show_agent_pins'
] as const;

/** The fields read off the prefs blob. Typed `unknown` because they are
 * checked at runtime: the blob is loaded from storage. Naming them here makes
 * a switch that is not a pref a compile error. */
export type PinUiConfigPrefs = Readonly<
	Record<'app_mode' | 'perf_tier' | (typeof PIN_UI_CONFIG_SWITCHES)[number], unknown>
>;

const ROUTE = /^\/[A-Za-z0-9/_.-]{0,119}$/;

export interface PinUiConfigInput {
	/** Page the pin was placed on; a query string or fragment is cut. */
	pathname: string;
	/** The UI prefs blob. Only the allowlisted fields are read. */
	prefs: PinUiConfigPrefs;
	/** Whether the page plays through the Rust engine (else Web Audio). */
	rustEngine: boolean;
}

function _slug(prefs: PinUiConfigPrefs, key: 'app_mode' | 'perf_tier'): string {
	const value = prefs[key];
	if (typeof value !== 'string' || value === '') {
		throw new Error(`pin ui config: pref ${key} must be a non-empty string`);
	}
	return value;
}

export function buildPinUiConfig(input: PinUiConfigInput): PinUiConfig {
	const route = input.pathname.split(/[?#]/, 1)[0];
	if (!ROUTE.test(route)) {
		throw new Error('pin ui config: route is not an app route');
	}
	const switches: Record<string, boolean> = {};
	for (const key of PIN_UI_CONFIG_SWITCHES) {
		const value = input.prefs[key];
		if (typeof value !== 'boolean') {
			throw new Error(`pin ui config: pref ${key} must be a boolean`);
		}
		switches[key] = value;
	}
	return {
		route,
		app_mode: _slug(input.prefs, 'app_mode'),
		engine_mode: input.rustEngine ? 'rust' : 'webaudio',
		perf_tier: _slug(input.prefs, 'perf_tier'),
		switches
	};
}
