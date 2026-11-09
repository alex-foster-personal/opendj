/**
 * Device-map discovery (#10560). Every map module in this directory registers
 * itself by exporting DEVICE_MAP_ENTRIES; this file finds them with Vite's
 * import.meta.glob, so a new controller adds its own map file and edits no
 * shared list. maps/index.ts builds the live WebMIDI registry (and so the
 * engine export) from the entries marked live. Practice replicas register
 * separately, one fragment per device in components/rb/practice/devices/, so
 * the lazy practice chunk loads only the maps a replica binds.
 *
 * Requirements (mini-PRD):
 *   ✔︎ Entries come back sorted by key, so the registry order and the
 *     generated engine export never depend on file-system or glob order.
 *     [if] two map files are added in either order [then] the registry is the same list
 *   ✔︎ Fail fast on a duplicate key.
 *     [if] two modules register the key FLX4_MAP [then] collecting the entries throws
 */

import type { DeviceMapEntry } from '$lib/rb/midi/midi-types';

export type DeviceMapModule = { DEVICE_MAP_ENTRIES?: readonly DeviceMapEntry[] };

const MAP_MODULES = import.meta.glob<DeviceMapModule>(['./*.ts', '!./index.ts', '!./entries.ts'], {
	eager: true
});

/** Every entry the given modules export, validated and sorted by key. Modules
 * that export no entries (shared helpers such as pioneer-deck-bindings.ts) add none. */
export function collectDeviceMapEntries(
	modules: Record<string, DeviceMapModule>
): readonly DeviceMapEntry[] {
	const entries = Object.values(modules).flatMap((m) => m.DEVICE_MAP_ENTRIES ?? []);
	const seen = new Set<string>();
	for (const entry of entries) {
		if (seen.has(entry.key)) throw new Error(`two device map modules register ${entry.key}`);
		seen.add(entry.key);
	}
	return [...entries].sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
}

export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = collectDeviceMapEntries(MAP_MODULES);
