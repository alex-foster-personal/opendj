/**
 * Device-map registry (build unit: FLX10 map owns this file).
 *
 * ONE place that knows every supported controller. The /performance page
 * calls registerAllDeviceMaps() once on mount (before or after initMidi(),
 * both fine - the core re-resolves connected ports on late registration).
 *
 * Ownership (conflict-free by design):
 *   - this file + ddj-flx10.ts: FLX10 unit
 *   - ddj-400.ts: DDJ-400 unit (owns its DDJ400_MAP export)
 *   - reloop-mixtour.ts: Mixtour unit (owns its RELOOP_MIXTOUR_MAP export)
 *
 * Requirements (mini-PRD):
 *   ✔︎ DEVICE_MAP_REGISTRY lists every DeviceMap the app supports.
 *   ✔︎ registerAllDeviceMaps() is idempotent BY DESIGN (documented, not
 *     hidden): /performance can remount across client-side navigations
 *     and the core has no unregister API, so "ensure registered" is the
 *     correct semantic. First call registers; later calls no-op.
 *     [if] registerAllDeviceMaps() runs twice [then] the core still holds
 *       exactly one copy of each map (no duplicate-map resolution drift)
 *   ✔︎ Registration itself stays fail-fast: registerDeviceMap throws on
 *     invalid nameMatch or duplicate bindings inside a map.
 */

import type { DeviceMap } from '$lib/rb/midi/midi-types';
import { registerDeviceMap } from '$lib/rb/midi/webmidi.svelte';
import { DDJ400_MAP } from './ddj-400';
import { FLX4_MAP } from './ddj-flx4';
import { FLX10_MAP } from './ddj-flx10';
import { RELOOP_MIXTOUR_MAP } from './reloop-mixtour';
import { RELOOP_MIXTOUR_PRO_MAP } from './reloop-mixtour-pro';

export const DEVICE_MAP_REGISTRY: readonly DeviceMap[] = [
	FLX10_MAP,
	DDJ400_MAP,
	RELOOP_MIXTOUR_PRO_MAP,
	RELOOP_MIXTOUR_MAP,
	FLX4_MAP
];

let _registered = false;

/** Register every supported device map with the WebMIDI core. Idempotent:
 * safe to call on every /performance mount (see mini-PRD). */
export function registerAllDeviceMaps(): void {
	if (_registered) return;
	_registered = true;
	for (const map of DEVICE_MAP_REGISTRY) registerDeviceMap(map);
}

/** TEST-ONLY: clear the idempotency latch (pair with _resetMidiForTests,
 * which drops the core's registered-map list). */
export function _resetMapsRegistryForTests(): void {
	_registered = false;
}
