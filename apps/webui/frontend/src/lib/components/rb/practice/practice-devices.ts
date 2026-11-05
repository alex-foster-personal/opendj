/**
 * Device maps a practice replica may bind to, keyed by the panel layout's `device_map`
 * name. Only maps that ship in DEVICE_MAP_REGISTRY belong here: a replica must fire what
 * the real controller fires on this machine.
 */

import type { DeviceMap } from '$lib/rb/midi/midi-types';
import { RB_API_BASE } from '$lib/rb/api-rb';
import { FLX4_MAP } from '$lib/rb/midi/maps/ddj-flx4';
import type { PanelLayout } from './practice-bindings';

export const PRACTICE_DEVICE_MAPS: Readonly<Record<string, DeviceMap>> = { FLX4_MAP };

export function practiceMapFor(layout: PanelLayout): DeviceMap {
	const map = PRACTICE_DEVICE_MAPS[layout.device_map];
	if (map === undefined) throw new Error(`no practice device map named ${layout.device_map}`);
	return map;
}

/** GET /api/v1/practice/devices/{device}: the panel layout the daemon serves. */
export async function fetchPanelLayout(device: string): Promise<PanelLayout> {
	const r = await fetch(`${RB_API_BASE}/api/v1/practice/devices/${encodeURIComponent(device)}`, {
		headers: { Accept: 'application/json' }
	});
	if (!r.ok) throw new Error(`practice layout ${device}: HTTP ${r.status} ${await r.text()}`);
	return (await r.json()) as PanelLayout;
}
