/**
 * Device-map registry (build unit: FLX10 map owns this file).
 *
 * The live registry: every map whose own module registers it with
 * `live: true` (DEVICE_MAP_ENTRIES, discovered by ./entries.ts). The
 * /performance page calls registerAllDeviceMaps() once on mount (before or
 * after initMidi(), both fine - the core re-resolves connected ports on late
 * registration). A new controller adds its own map file and edits nothing here.
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
import { DEVICE_MAP_ENTRIES } from './entries';

/** Every live map, in entry-key order (the engine export follows this order). */
export const DEVICE_MAP_REGISTRY: readonly DeviceMap[] = DEVICE_MAP_ENTRIES.filter(
	(e) => e.live
).map((e) => e.map);

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
