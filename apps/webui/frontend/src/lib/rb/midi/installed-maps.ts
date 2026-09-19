/**
 * Fetch controller maps the daemon has installed and register them at runtime
 * (build unit: midi installed maps).
 *
 * This is the client half of /api/v1/midi/maps. A map onboarded in the app is
 * a DOCUMENT the daemon owns, not a module in the bundle, so it reaches the
 * WebMIDI core the same way a builtin does but in the 'installed' tier, where
 * it shadows its builtin twin (webmidi.svelte.ts, DeviceMapTier).
 *
 * The wire shape is deliberately identical to DeviceMap plus a per-binding
 * `provenance`, so there is no key translation here: the conversion strips
 * provenance and hands the rest straight to registerDeviceMap. Provenance is
 * kept alongside, keyed by map id, because the UI has to be able to say WHERE
 * a binding's numbers came from.
 *
 * Fail-fast: the daemon has already validated every document against the same
 * action union (routes/midi_maps.py), so a document that fails here is a
 * contract break, not user error, and it throws rather than being skipped.
 *
 * Requirements (mini-PRD):
 *   ✔︎ loadInstalledDeviceMaps() registers every installed map in the
 *     'installed' tier and returns what it registered.
 *     [if] the daemon serves a map matching a builtin's port name [then] the
 *       installed one wins and the builtin stays registered underneath
 *   ✔︎ Re-running it replaces rather than throwing, so a wizard save can take
 *     effect without a page reload.
 *     [if] a second load throws 'already registered' [then ⛔️] broken
 *   ✔︎ A map the daemon removed is unregistered on the next load, not left
 *     behind.
 *     [if] an uninstalled map still resolves after a reload [then ⛔️] broken
 *   ✔︎ Network and contract failures propagate. No silent empty result.
 *   ✔︎ A reload is all-or-nothing: every document is fetched AND validated
 *     (validateDeviceMap) before the runtime registry is touched, so a
 *     failure partway through a batch (a daemon race, a network blip, or a
 *     document whose nameMatch the browser's RegExp rejects even though the
 *     daemon's necessarily-partial deny list accepted it) leaves the
 *     PREVIOUS generation fully registered rather than a mix of old and new
 *     maps, or none at all.
 *     [if] one document's fetch fails after another's already reflects the
 *       new generation [then ⛔️] broken
 *     [if] one document's nameMatch fails validateDeviceMap after the
 *       previous generation has already been unregistered [then ⛔️] broken
 *     [if] two installed documents claim the same nameMatch (a raced PUT past
 *       the daemon's own clash check) after the previous generation has
 *       already been unregistered [then ⛔️] broken
 */

import { RB_API_BASE, RbApiError } from '$lib/rb/api-rb';
import type { DeviceMap, MidiBinding } from '$lib/rb/midi/midi-types';
import {
	listDeviceMaps,
	registerDeviceMap,
	unregisterDeviceMap,
	validateDeviceMap
} from '$lib/rb/midi/webmidi.svelte';

/** Where one binding's wire numbers came from. Mirrors the daemon's model. */
export interface BindingProvenance {
	tier: 'vendor-pdf' | 'mixxx' | 'learned' | 'hand';
	cite: string;
	verified: boolean;
}

export interface InstalledMapDoc {
	schemaVersion: 1;
	id: string;
	vendor: string;
	model: string;
	nameMatch: string;
	bindings: (MidiBinding & { provenance: BindingProvenance })[];
}

export interface InstalledMapSummary {
	id: string;
	vendor: string;
	model: string;
	nameMatch: string;
	bindingCount: number;
	provenance: Record<string, number>;
}

/** Provenance for the maps currently registered, by map id then binding
 * index. The runtime DeviceMap deliberately does not carry it - it is UI
 * metadata, not dispatch data. */
export const installedProvenance = new Map<string, BindingProvenance[]>();

/** nameMatch of every map this module registered, so a later load can remove
 * the ones the daemon no longer serves. */
const _registered = new Map<string, string>(); // id -> nameMatch

async function _getJson<T>(path: string): Promise<T> {
	const r = await fetch(`${RB_API_BASE}${path}`, { headers: { Accept: 'application/json' } });
	if (!r.ok) {
		const body = (await r.json()) as { detail?: { code?: string; message?: string } };
		throw new RbApiError(
			r.status,
			body.detail?.code ?? `HTTP_${r.status}`,
			body.detail?.message ?? r.statusText
		);
	}
	return (await r.json()) as T;
}

/** Strip the UI-only provenance field; what is left IS a DeviceMap. */
export function deviceMapFromDoc(doc: InstalledMapDoc): DeviceMap {
	return {
		vendor: doc.vendor,
		nameMatch: doc.nameMatch,
		bindings: doc.bindings.map(({ provenance: _p, ...binding }) => binding)
	};
}

/** Convert and validate one complete installed-map generation before the
 * registry changes. Exported so its race-safe batch invariant is directly
 * testable with real document fixtures. */
export function validateInstalledMapBatch(docs: InstalledMapDoc[]): DeviceMap[] {
	const newMaps = docs.map(deviceMapFromDoc);
	for (const map of newMaps) validateDeviceMap(map);

	const byNameMatch = new Map<string, string>(); // nameMatch -> map id
	for (const [at, map] of newMaps.entries()) {
		const clashId = byNameMatch.get(map.nameMatch);
		if (clashId !== undefined) {
			throw new Error(
				`loadInstalledDeviceMaps: installed maps '${clashId}' and '${docs[at].id}' both claim nameMatch '${map.nameMatch}'`
			);
		}
		byNameMatch.set(map.nameMatch, docs[at].id);
	}
	return newMaps;
}

/** Fetch every installed map and register it in the 'installed' tier.
 * Idempotent by replacement: safe to call again after a wizard save.
 * All-or-nothing: every document is fetched (network phase) before any
 * runtime registration changes (commit phase), so a failure partway
 * through the fetches never applies a partial generation. */
export async function loadInstalledDeviceMaps(): Promise<DeviceMap[]> {
	const { maps } = await _getJson<{ maps: InstalledMapSummary[] }>('/api/v1/midi/maps');
	const docs: InstalledMapDoc[] = [];
	for (const summary of maps) {
		docs.push(await _getJson<InstalledMapDoc>(`/api/v1/midi/maps/${summary.id}`));
	}
	// Validate every map BEFORE touching the registry, same as the fetch
	// phase above: the daemon's nameMatch deny list cannot invoke the
	// browser's own regex engine (routes/midi_maps.py), so a document it
	// accepted can still fail validateDeviceMap() here (an atomic group
	// compiles under Python's re but throws in RegExp). Validating the whole
	// batch first means that throw happens before the unregister loop below,
	// so it leaves the previous generation intact instead of wiping it.
	// The daemon's own clash check (_installed_name_match_clash,
	// routes/midi_maps.py) reads the maps directory before it writes, so two
	// concurrent PUTs for DIFFERENT ids can each see no clash and both land,
	// leaving two documents on disk sharing one nameMatch even though the
	// daemon rejects that in the steady state. registerDeviceMap() throws on
	// a same-tier nameMatch collision, and it would do so HALFWAY through the
	// commit loop below - after the previous generation is already
	// unregistered - which is exactly the partial-generation failure this
	// reload exists to prevent. Catch the clash here, against the batch
	// itself, before the registry is touched at all.
	const newMaps = validateInstalledMapBatch(docs);

	// Every fetch and validation succeeded: commit the new generation.
	// Unregister the whole previous generation first, so a renamed id cannot
	// briefly register twice on the same nameMatch.
	for (const [id, nameMatch] of [..._registered]) {
		unregisterDeviceMap(nameMatch, 'installed');
		_registered.delete(id);
		installedProvenance.delete(id);
	}

	const loaded: DeviceMap[] = [];
	for (const [at, doc] of docs.entries()) {
		const map = newMaps[at];
		registerDeviceMap(map, 'installed');
		_registered.set(doc.id, map.nameMatch);
		installedProvenance.set(
			doc.id,
			doc.bindings.map((b) => b.provenance)
		);
		loaded.push(map);
	}
	return loaded;
}

/** Which builtin, if any, an installed map is currently shadowing. Lets the
 * device row say so out loud instead of silently overriding. */
export function shadowedBuiltin(nameMatch: string): DeviceMap | null {
	const builtin = listDeviceMaps().find((e) => e.tier === 'builtin' && e.map.nameMatch === nameMatch);
	return builtin?.map ?? null;
}

/** TEST-ONLY: forget what this module registered (pair with
 * _resetMidiForTests, which drops the core's registry). */
export function _resetInstalledMapsForTests(): void {
	_registered.clear();
	installedProvenance.clear();
}
