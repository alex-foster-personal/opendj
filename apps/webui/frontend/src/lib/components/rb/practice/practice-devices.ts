/**
 * Device maps a practice replica may bind to, keyed by the panel layout's `device_map`
 * name. Each replica registers itself with one fragment, ./devices/<device>.ts, exporting
 * PRACTICE_MAPS; this file globs them, so a new replica edits nothing here (#10560). Only
 * the fragments' maps reach the lazy practice chunk. Every one must also be a
 * self-registered map ($lib/rb/midi/maps/entries.ts), live or hints-only.
 */

import type { DeviceMap } from '$lib/rb/midi/midi-types';
import { RB_API_BASE } from '$lib/rb/api-rb';
import type { PanelLayout } from './practice-bindings';

export type PracticeFragment = { PRACTICE_MAPS: Readonly<Record<string, DeviceMap>> };

const FRAGMENTS = import.meta.glob<PracticeFragment>('./devices/*.ts', { eager: true });

/** Merge the fragments' maps; fail fast when two fragments claim one `device_map` name. */
export function mergePracticeFragments(
	fragments: Record<string, PracticeFragment>
): Readonly<Record<string, DeviceMap>> {
	const out: Record<string, DeviceMap> = {};
	for (const [path, fragment] of Object.entries(fragments)) {
		for (const [name, map] of Object.entries(fragment.PRACTICE_MAPS)) {
			if (name in out) throw new Error(`${path} registers ${name} a second time`);
			out[name] = map;
		}
	}
	return out;
}

export const PRACTICE_DEVICE_MAPS: Readonly<Record<string, DeviceMap>> =
	mergePracticeFragments(FRAGMENTS);

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
