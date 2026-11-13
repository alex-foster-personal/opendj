/**
 * Export and import an installed MIDI map as a JSON file (CTRL-10093).
 *
 * Pure helpers behind the MIDI panel's Export / Import map file actions,
 * mimicking rekordbox's MIDI window EXPORT / IMPORT of the mapping file and
 * Mixxx loading a mapping from disk. The file IS the `MidiMapDoc` the daemon
 * serves from `GET /api/v1/midi/maps/{id}`, unchanged apart from key order
 * (`schemaVersion` first), so an exported file is a valid PUT body.
 *
 * Requirements (mini-PRD):
 *   - serialiseMapDoc(doc) puts schemaVersion first and parses back equal.
 *     [if] JSON.parse(serialiseMapDoc(doc)) is not deep-equal to doc [then] broken
 *   - parseMapFile(text) rejects a file without schemaVersion 1, id,
 *     nameMatch or bindings with a named reason, before any network call.
 *     [if] a file missing bindings parses ok [then] broken
 *   - diffMapDocs(a, b) is pure and lists bindings added, removed and changed
 *     by source key.
 *     [if] identical docs yield a non-empty diff [then] broken
 */

import type { InstalledMapDoc } from '$lib/rb/midi/installed-maps';

type DocBinding = InstalledMapDoc['bindings'][number];

/** The download name for an exported map. */
export function exportFileName(id: string): string {
	return `${id}.opendj-midi-map.json`;
}

/** Serialise a map document for download with `schemaVersion` as the first key. */
export function serialiseMapDoc(doc: InstalledMapDoc): string {
	const { schemaVersion, ...rest } = doc;
	return `${JSON.stringify({ schemaVersion, ...rest }, null, 2)}\n`;
}

export type ParseMapFileResult = { ok: true; doc: InstalledMapDoc } | { ok: false; reason: string };

function _isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Parse and shape-check an imported map file; never touches the network. */
export function parseMapFile(text: string): ParseMapFileResult {
	let raw: unknown;
	try {
		raw = JSON.parse(text);
	} catch (err) {
		return { ok: false, reason: `not JSON: ${(err as Error).message}` };
	}
	if (!_isRecord(raw)) return { ok: false, reason: 'not a JSON object' };
	if (raw.schemaVersion !== 1) {
		return { ok: false, reason: 'schemaVersion must be 1' };
	}
	if (typeof raw.id !== 'string' || raw.id === '') {
		return { ok: false, reason: 'missing id' };
	}
	if (typeof raw.nameMatch !== 'string' || raw.nameMatch === '') {
		return { ok: false, reason: 'missing nameMatch' };
	}
	if (!Array.isArray(raw.bindings)) {
		return { ok: false, reason: 'missing bindings' };
	}
	for (const [at, binding] of raw.bindings.entries()) {
		if (!_isRecord(binding) || !_isRecord(binding.source) || !_isRecord(binding.action)) {
			return { ok: false, reason: `binding ${at} has no source or action` };
		}
	}
	return { ok: true, doc: raw as unknown as InstalledMapDoc };
}

/** Identify a binding by its wire source and shift layer, the key a diff matches on. */
export function bindingSourceKey(binding: DocBinding): string {
	const { ch, kind, id } = binding.source;
	return `${kind}:${ch}:${id}${binding.shift === true ? ':shift' : ''}`;
}

/** Stable JSON with sorted object keys, so key order never reads as a change. */
function _stable(value: unknown): string {
	if (Array.isArray(value)) return `[${value.map(_stable).join(',')}]`;
	if (_isRecord(value)) {
		const keys = Object.keys(value).sort();
		return `{${keys.map((k) => `${JSON.stringify(k)}:${_stable(value[k])}`).join(',')}}`;
	}
	return JSON.stringify(value);
}

export interface MapDocDiff {
	added: DocBinding[];
	removed: DocBinding[];
	changed: { key: string; before: DocBinding; after: DocBinding }[];
}

/** Diff two map documents' bindings by source key: what `b` adds, removes and changes from `a`. */
export function diffMapDocs(a: InstalledMapDoc, b: InstalledMapDoc): MapDocDiff {
	const before = new Map(a.bindings.map((x) => [bindingSourceKey(x), x] as const));
	const after = new Map(b.bindings.map((x) => [bindingSourceKey(x), x] as const));
	const diff: MapDocDiff = { added: [], removed: [], changed: [] };
	for (const [key, next] of after) {
		const prev = before.get(key);
		if (prev === undefined) diff.added.push(next);
		else if (_stable(prev) !== _stable(next)) diff.changed.push({ key, before: prev, after: next });
	}
	for (const [key, prev] of before) {
		if (!after.has(key)) diff.removed.push(prev);
	}
	return diff;
}

/** True when a diff has nothing to show. */
export function isEmptyDiff(diff: MapDocDiff): boolean {
	return diff.added.length === 0 && diff.removed.length === 0 && diff.changed.length === 0;
}

/** Copy an import with a new id and nameMatch, for retrying after a 422 nameMatch clash. */
export function renameImport(doc: InstalledMapDoc, id: string, nameMatch: string): InstalledMapDoc {
	return { ...doc, id, nameMatch };
}
