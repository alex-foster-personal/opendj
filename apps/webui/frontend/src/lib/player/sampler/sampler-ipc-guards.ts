/**
 * Wire guards for the edge sampler's commands on the performance bus
 * ((private path) section 10, agent parity; #10786).
 *
 * Requirements (mini-PRD):
 *   ✔︎ every sampler_* command is parsed with exact keys; an unknown field is refused
 *     [if] sampler_enabled carries an extra key [then ⛔️]
 *     [if] sampler_pin has no pinned boolean [then ⛔️]
 *   ✔︎ a slot is addressed by exactly one of slot_id or ref
 *     [if] both slot_id and ref are sent [then ⛔️]
 *     [if] neither is sent [then ⛔️]
 *     [if] ref is not S<n>-<nn> or R-<nn> [then ⛔️]
 *   ✔︎ the alternate forms ({all}, {stop}) are refused when mixed with a slot address
 *     [if] sampler_stop sends all with a ref [then ⛔️]
 *   ✔︎ sampler_set_page takes exactly one of set or a non-zero delta
 *     [if] set and delta are both sent, or delta is 0 [then ⛔️]
 *   ✔︎ search, filter, results, slot update, set name, capture, editor and delete (port map row 27)
 *     [if] sampler_filter names a filter outside broken|remixes|vocals|offline|compatible [then ⛔️]
 *     [if] sampler_edit sends close with a ref [then ⛔️]
 *     [if] sampler_capture names deck 5 [then ⛔️]
 *   ✔︎ settings patches are validated against the sampler-types enums and ranges
 *     [if] level is 101, downbeat_offset is 4 or 1.5, or repeat is 3 [then ⛔️]
 *     [if] settings is empty or names an unknown field [then ⛔️]
 *   → starter commands (the starter set is an open decision)
 * Pure: no Svelte state, so node tests load it directly.
 */

import { NO_FILTERS, type EdgeFilters } from './edge-search';
import { REPEAT_CYCLE } from './sampler-repeat';
import { parseRef } from './sampler-store-logic';
import type { Edge, SlotSettings } from './sampler-types';

type Rec = Record<string, unknown>;

export type SamplerCommand =
	| { type: 'sampler_enabled'; on: boolean }
	| { type: 'sampler_trigger'; slot_id?: string; ref?: string }
	| { type: 'sampler_stop'; slot_id?: string; ref?: string; all?: true }
	| { type: 'sampler_release'; slot_id?: string; ref?: string }
	| { type: 'sampler_preview'; slot_id?: string; ref?: string; stop?: true }
	| { type: 'sampler_set_page'; edge: Edge; set?: number; delta?: number }
	| { type: 'sampler_pin'; slot_id?: string; ref?: string; pinned: boolean }
	| { type: 'sampler_search'; edge: Edge; query: string }
	| { type: 'sampler_filter'; edge: Edge; filter: keyof EdgeFilters; on: boolean }
	| { type: 'sampler_results'; edge: Edge; show: boolean }
	| {
			type: 'sampler_slot_update';
			slot_id?: string;
			ref?: string;
			name?: string;
			description?: string;
			settings?: Partial<SlotSettings>;
	  }
	| { type: 'sampler_set_name'; set_id: string; name: string; description: string }
	| { type: 'sampler_capture'; deck: 1 | 2 | 3 | 4; set: number; index: number }
	| { type: 'sampler_edit'; slot_id?: string; ref?: string; close?: true }
	| { type: 'sampler_delete'; slot_id?: string; ref?: string };

const SAMPLER_COMMAND_TYPES: readonly SamplerCommand['type'][] = [
	'sampler_enabled',
	'sampler_trigger',
	'sampler_stop',
	'sampler_release',
	'sampler_preview',
	'sampler_set_page',
	'sampler_pin',
	'sampler_search',
	'sampler_filter',
	'sampler_results',
	'sampler_slot_update',
	'sampler_set_name',
	'sampler_capture',
	'sampler_edit',
	'sampler_delete'
];

const SETTINGS_ENUMS: Record<'launch' | 'key' | 'tempo' | 'mode', readonly string[]> = {
	launch: ['bar', 'beat', '4bars', 'off'],
	key: ['auto', 'master_tempo', 'off'],
	tempo: ['match', 'own'],
	mode: ['loop', 'one_shot', 'gate']
};
const SETTINGS_KEYS = [...Object.keys(SETTINGS_ENUMS), 'repeat', 'downbeat_offset', 'level'];

//----- helpers

function _exact(r: Rec, keys: readonly string[]): void {
	const bad = Object.keys(r).filter((k) => !keys.includes(k));
	if (bad.length > 0)
		throw new TypeError(`${String(r.type)} has unexpected fields: ${bad.join(', ')}`);
}

function _bool(name: string, v: unknown): boolean {
	if (typeof v !== 'boolean') throw new TypeError(`${name} must be boolean`);
	return v;
}

function _text(name: string, v: unknown, allowEmpty = false): string {
	if (typeof v !== 'string' || (!allowEmpty && v.trim() === ''))
		throw new TypeError(`${name} must be a ${allowEmpty ? 'string' : 'non-empty string'}`);
	return v;
}

function _int(name: string, v: unknown, min: number, max: number): number {
	if (typeof v !== 'number' || !Number.isInteger(v) || v < min || v > max) {
		throw new RangeError(`${name} must be an integer within ${min}..${max}; got ${String(v)}`);
	}
	return v;
}

function _edge(v: unknown): Edge {
	if (v !== 'L' && v !== 'R') throw new RangeError(`edge must be L or R; got ${String(v)}`);
	return v;
}

function _true(name: string, v: unknown): true {
	if (v !== true) throw new TypeError(`${name} may only be true`);
	return true;
}

/** Exactly one of slot_id or ref. */
function _address(r: Rec): { slot_id: string } | { ref: string } {
	const hasId = r.slot_id !== undefined;
	const hasRef = r.ref !== undefined;
	if (hasId === hasRef)
		throw new TypeError(`${String(r.type)} needs exactly one of slot_id or ref`);
	if (hasId) return { slot_id: _text('slot_id', r.slot_id) };
	const ref = _text('ref', r.ref);
	if (parseRef(ref) === null) throw new RangeError(`ref must look like S1-05 or R-03; got ${ref}`);
	return { ref };
}

/** A partial SlotSettings: each field in its sampler-types domain, at least one field. */
function _settings(v: unknown): Partial<SlotSettings> {
	if (typeof v !== 'object' || v === null || Array.isArray(v))
		throw new TypeError('settings must be an object');
	const r = v as Rec;
	const bad = Object.keys(r).filter((k) => !SETTINGS_KEYS.includes(k));
	if (bad.length > 0) throw new TypeError(`settings has unexpected fields: ${bad.join(', ')}`);
	const out: Rec = {};
	for (const [k, allowed] of Object.entries(SETTINGS_ENUMS)) {
		if (r[k] === undefined) continue;
		if (typeof r[k] !== 'string' || !allowed.includes(r[k]))
			throw new RangeError(
				`settings.${k} must be one of ${allowed.join('|')}; got ${String(r[k])}`
			);
		out[k] = r[k];
	}
	if (r.repeat !== undefined) {
		if (!(REPEAT_CYCLE as readonly unknown[]).includes(r.repeat))
			throw new RangeError(
				`settings.repeat must be one of ${REPEAT_CYCLE.join('|')}; got ${String(r.repeat)}`
			);
		out.repeat = r.repeat;
	}
	if (r.downbeat_offset !== undefined)
		out.downbeat_offset = _int('settings.downbeat_offset', r.downbeat_offset, -3, 3);
	if (r.level !== undefined) out.level = _int('settings.level', r.level, 0, 100);
	if (Object.keys(out).length === 0) throw new TypeError('settings must change at least one field');
	return out as Partial<SlotSettings>;
}

//----- parse

/** True when `type` names one of the sampler commands this module parses. */
export function isSamplerCommandType(type: unknown): type is SamplerCommand['type'] {
	return (SAMPLER_COMMAND_TYPES as readonly unknown[]).includes(type);
}

/** Validate one sampler_* wire record into a typed command; throws on any malformed field. */
export function parseSamplerCommand(r: Rec): SamplerCommand {
	const type = r.type;
	if (type === 'sampler_enabled') {
		_exact(r, ['type', 'on']);
		return { type, on: _bool('on', r.on) };
	}
	if (type === 'sampler_trigger' || type === 'sampler_release' || type === 'sampler_delete') {
		_exact(r, ['type', 'slot_id', 'ref']);
		return { type, ..._address(r) };
	}
	if (type === 'sampler_stop' || type === 'sampler_preview' || type === 'sampler_edit') {
		const flag = { sampler_stop: 'all', sampler_preview: 'stop', sampler_edit: 'close' }[type];
		_exact(r, ['type', 'slot_id', 'ref', flag]);
		if (r[flag] !== undefined) {
			if (r.slot_id !== undefined || r.ref !== undefined)
				throw new TypeError(`${type} takes ${flag} or a slot, not both`);
			return { type, [flag]: _true(flag, r[flag]) } as SamplerCommand;
		}
		return { type, ..._address(r) };
	}
	if (type === 'sampler_set_page') {
		_exact(r, ['type', 'edge', 'set', 'delta']);
		const edge = _edge(r.edge);
		if ((r.set === undefined) === (r.delta === undefined))
			throw new TypeError('sampler_set_page needs exactly one of set or delta');
		if (r.set !== undefined) return { type, edge, set: _int('set', r.set, 0, 9999) };
		const delta = _int('delta', r.delta, -9999, 9999);
		if (delta === 0) throw new RangeError('delta must not be 0');
		return { type, edge, delta };
	}
	if (type === 'sampler_pin') {
		_exact(r, ['type', 'slot_id', 'ref', 'pinned']);
		return { type, ..._address(r), pinned: _bool('pinned', r.pinned) };
	}
	if (type === 'sampler_search') {
		_exact(r, ['type', 'edge', 'query']);
		return { type, edge: _edge(r.edge), query: _text('query', r.query, true) };
	}
	if (type === 'sampler_filter') {
		_exact(r, ['type', 'edge', 'filter', 'on']);
		if (typeof r.filter !== 'string' || !(r.filter in NO_FILTERS))
			throw new RangeError(
				`filter must be one of ${Object.keys(NO_FILTERS).join('|')}; got ${String(r.filter)}`
			);
		const filter = r.filter as keyof EdgeFilters;
		return { type, edge: _edge(r.edge), filter, on: _bool('on', r.on) };
	}
	if (type === 'sampler_results') {
		_exact(r, ['type', 'edge', 'show']);
		return { type, edge: _edge(r.edge), show: _bool('show', r.show) };
	}
	if (type === 'sampler_slot_update') {
		_exact(r, ['type', 'slot_id', 'ref', 'name', 'description', 'settings']);
		const out: SamplerCommand = { type, ..._address(r) };
		if (r.name !== undefined) out.name = _text('name', r.name, true);
		if (r.description !== undefined) out.description = _text('description', r.description, true);
		if (r.settings !== undefined) out.settings = _settings(r.settings);
		if (out.name === undefined && out.description === undefined && out.settings === undefined)
			throw new TypeError('sampler_slot_update needs name, description or settings');
		return out;
	}
	if (type === 'sampler_set_name') {
		_exact(r, ['type', 'set_id', 'name', 'description']);
		return {
			type,
			set_id: _text('set_id', r.set_id),
			name: _text('name', r.name),
			description: _text('description', r.description, true)
		};
	}
	if (type === 'sampler_capture') {
		_exact(r, ['type', 'deck', 'set', 'index']);
		return {
			type,
			deck: _int('deck', r.deck, 1, 4) as 1 | 2 | 3 | 4,
			set: _int('set', r.set, 0, 9999),
			index: _int('index', r.index, 0, 9999)
		};
	}
	throw new TypeError(`not a sampler command: ${String(type)}`);
}
