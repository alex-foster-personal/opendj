/**
 * Native Instruments Traktor Kontrol S4 MK3: practice-only map entry (epic #9366, ADR-0137).
 *
 * THIS UNIT SENDS NO MIDI. The S4 MK3 drives Traktor over HID (Native Instruments'
 * own protocol); the official manual ("TRAKTOR KONTROL S4 MK3 Manual English",
 * sections 5.1 to 5.3) lists every control but no MIDI mode and no message list,
 * and the maker publishes no MIDI implementation chart for it. Nothing here is a
 * transcription of a wire, because there is no wire to transcribe.
 *
 * The panel declares every control's wire unavailable (`midi: null`); `resolveControl`
 * returns `unwired` for those without consulting the map. This map exists so the
 * practice registry can name the device. `bindings` is empty: nothing fires and
 * nothing lights.
 *
 * NOT IN DEVICE_MAP_REGISTRY, on purpose: the registry matches WebMIDI port names
 * and exports to the Rust engine; a map whose keys match no hardware message has
 * no place there, and the export test rightly refuses a map with no bindings.
 * It lives in PRACTICE_DEVICE_MAPS only. If Native Instruments ever publishes a
 * MIDI list, put the published wires in the panel builder and bind them here.
 */

import type { DeviceMap, DeviceMapEntry } from '$lib/rb/midi/midi-types';

export const TRAKTOR_S4_MK3_MAP: DeviceMap = {
	vendor: 'Native Instruments',
	// Never registered with the WebMIDI core (see the header); kept as the model name so an
	// accidental registration would at least be readable in the learn log.
	nameMatch: 'Traktor Kontrol S4 MK3',
	bindings: []
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'TRAKTOR_S4_MK3_MAP', map: TRAKTOR_S4_MK3_MAP, live: false }
];
