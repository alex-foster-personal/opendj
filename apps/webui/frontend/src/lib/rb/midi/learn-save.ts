/**
 * MIDI Learn save (#9781): put a learned binding into the user's map copy via
 * POST /api/v1/midi/maps/{id}/learned-binding.
 *
 * Pure apart from the injected fetch: no WebMIDI, runes or engine imports, so
 * midi-ui-state can load it on first paint without pulling the engine chunk.
 *
 * Requirements (mini-PRD):
 *   [if] the device runs a shipped built-in map [then] the save targets a new
 *     `user-<port>` map seeded from a copy of it, never the built-in, [else stop].
 *   [if] the daemon answers 409 MIDI_LEARN_CONFLICT [then] confirmReplace is asked
 *     and only a yes resends with replace, [else stop].
 */

import { API_BASE } from '$lib/api/base';
import type { DeviceMap, MidiAction, MidiBinding } from './midi-types';

/** One command the panel's Learn picker offers. */
export interface LearnableCommand {
	label: string;
	action: MidiAction;
}

/** The performance commands the MIDI panel can learn onto a control. */
export const LEARNABLE_COMMANDS: readonly LearnableCommand[] = [
	{ label: 'Deck 1 Play/Pause', action: { type: 'deck_play_toggle', deck: 1 } },
	{ label: 'Deck 2 Play/Pause', action: { type: 'deck_play_toggle', deck: 2 } },
	{ label: 'Deck 1 Cue', action: { type: 'deck_cue', deck: 1 } },
	{ label: 'Deck 2 Cue', action: { type: 'deck_cue', deck: 2 } },
	{ label: 'Deck 1 Sync', action: { type: 'deck_sync_toggle', deck: 1 } },
	{ label: 'Deck 2 Sync', action: { type: 'deck_sync_toggle', deck: 2 } },
	{ label: 'Browse encoder', action: { type: 'browse_encoder' } },
	{ label: 'Load to Deck 1', action: { type: 'browse_load', deck: 1 } },
	{ label: 'Load to Deck 2', action: { type: 'browse_load', deck: 2 } }
];

/** Where a learned binding is saved: an installed user map, or a new copy. */
export interface LearnSaveTarget {
	mapId: string;
	/** Seed for a map that is not installed yet (built-in copy or empty). */
	template: Record<string, unknown> | null;
}

export type LearnSaveResult =
	| { status: 'saved'; mapId: string }
	| { status: 'cancelled'; mapId: string; existing: MidiBinding };

/** Lowercase slug of a port name for a user map id (`user-<slug>`). */
export function userMapIdFor(portName: string): string {
	const slug = portName
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, '-')
		.replace(/^-+|-+$/g, '');
	return `user-${slug || 'controller'}`;
}

/** Escape a port name for an exact, case-insensitive nameMatch regex. */
function _exactNameMatch(portName: string): string {
	return `^${portName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`;
}

/** Choose the user map a learned binding goes into, never a shipped built-in. */
export function learnSaveTarget(
	portName: string,
	map: DeviceMap | null,
	installedId: string | null
): LearnSaveTarget {
	if (installedId !== null) return { mapId: installedId, template: null };
	const provenance = {
		tier: 'hand',
		cite: `copied from the built-in ${map?.vendor ?? ''} map`,
		verified: false
	};
	return {
		mapId: userMapIdFor(portName),
		template: {
			schemaVersion: 1,
			vendor: map?.vendor ?? 'User',
			model: portName,
			nameMatch: map?.nameMatch ?? _exactNameMatch(portName),
			bindings: (map?.bindings ?? []).map((b) => ({ ...b, provenance })),
			leds: (map?.leds ?? []).map((rule) => ({ ...rule, provenance })),
			meters: (map?.meters ?? []).map((meter) => ({ ...meter, provenance })),
			init: (map?.init ?? []).map((message) => ({ message, provenance }))
		}
	};
}

type FetchLike = (url: string, init: RequestInit) => Promise<Response>;

/** POST one learned binding; on a conflict ask confirmReplace and resend only on yes. */
export async function saveLearnedBinding(
	target: LearnSaveTarget,
	binding: MidiBinding,
	confirmReplace: (existing: MidiBinding) => boolean | Promise<boolean>,
	fetchImpl: FetchLike = (url, init) => fetch(url, init)
): Promise<LearnSaveResult> {
	const url = `${API_BASE}/api/v1/midi/maps/${encodeURIComponent(target.mapId)}/learned-binding`;
	const post = (replace: boolean) =>
		fetchImpl(url, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
			body: JSON.stringify({ binding, replace, template: target.template })
		});
	let r = await post(false);
	if (r.status === 409) {
		const body = (await r.json()) as { detail: { existing: MidiBinding } };
		const existing = body.detail.existing;
		if (!(await confirmReplace(existing))) {
			return { status: 'cancelled', mapId: target.mapId, existing };
		}
		r = await post(true);
	}
	if (!r.ok) {
		const body = (await r.json().catch(() => ({}))) as { detail?: { message?: string } };
		throw new Error(
			`MIDI Learn save failed (${r.status}): ${body.detail?.message ?? r.statusText}`
		);
	}
	return { status: 'saved', mapId: target.mapId };
}
