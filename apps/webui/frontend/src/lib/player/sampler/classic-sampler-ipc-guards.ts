/**
 * Wire guards for the classic sampler unit's commands on the performance bus
 * (#9987 and #9989, agent parity).
 *
 * Named `sampler_unit_*`, not the issues' `sampler_load` / `sampler_trigger` / `sampler_stop`:
 * the edge sampler already owns `sampler_trigger`, `sampler_stop` and `sampler_release` on the
 * same bus with a slot_id/ref address, so the classic 4 x 16 unit gets its own prefix and a
 * `{bank, slot}` address (0-based, bank 0..3, slot 0..15) instead of overloading those.
 *
 * Requirements (mini-PRD):
 *   ✔︎ every sampler_unit_* command is parsed with exact keys; an unknown field is refused
 *     [if] sampler_unit_trigger carries a ref [then ⛔️]
 *   ✔︎ a slot address is whole numbers inside the unit
 *     [if] bank is 4 or slot is 1.5 [then ⛔️]
 *   ✔︎ a load names at most one of stable_id or path; neither clears the slot; duration_ms (the
 *     whole-source length a trigger needs) is optional and positive
 *     [if] both stable_id and path are sent [then ⛔️]
 *   ✔︎ trigger held:false is the pad release; stop takes both or neither of bank and slot
 *     [if] sampler_unit_stop sends a bank with no slot [then ⛔️]
 *   ✔︎ mode, slot gain and master gain stay in their domains
 *     [if] mode is reverse, gain_db is 7, or master gain_db is 1 [then ⛔️]
 * Pure: no Svelte state, so node tests load it directly.
 */

import type { BankMode } from './api-sampler-banks';
import {
	BANK_MODES,
	GAIN_DB_MAX,
	GAIN_DB_MIN,
	MASTER_GAIN_DB_MAX,
	MASTER_GAIN_DB_MIN,
	slotAddress,
	type SlotAddress
} from './classic-sampler-logic';

type Rec = Record<string, unknown>;

export type ClassicSamplerCommand =
	| {
			type: 'sampler_unit_load';
			bank: number;
			slot: number;
			stable_id?: string;
			path?: string;
			title?: string;
			duration_ms?: number;
	  }
	| { type: 'sampler_unit_trigger'; bank: number; slot: number; held?: boolean }
	| { type: 'sampler_unit_stop'; bank?: number; slot?: number }
	| { type: 'sampler_unit_mode'; bank: number; slot: number; mode: BankMode }
	| { type: 'sampler_unit_gain'; bank: number; slot: number; gain_db: number }
	| { type: 'sampler_unit_master_gain'; gain_db: number };

const CLASSIC_SAMPLER_COMMAND_TYPES: readonly ClassicSamplerCommand['type'][] = [
	'sampler_unit_load',
	'sampler_unit_trigger',
	'sampler_unit_stop',
	'sampler_unit_mode',
	'sampler_unit_gain',
	'sampler_unit_master_gain'
];

//----- helpers

function _exact(r: Rec, keys: readonly string[]): void {
	const bad = Object.keys(r).filter((k) => !keys.includes(k));
	if (bad.length > 0)
		throw new TypeError(`${String(r.type)} has unexpected fields: ${bad.join(', ')}`);
}

function _address(r: Rec): SlotAddress {
	if (typeof r.bank !== 'number' || typeof r.slot !== 'number')
		throw new TypeError(`${String(r.type)} needs a numeric bank and slot`);
	return slotAddress(r.bank, r.slot);
}

function _text(name: string, v: unknown): string {
	if (typeof v !== 'string' || v.trim() === '')
		throw new TypeError(`${name} must be a non-empty string`);
	return v;
}

function _db(name: string, v: unknown, min: number, max: number): number {
	if (typeof v !== 'number' || !Number.isFinite(v) || v < min || v > max)
		throw new RangeError(`${name} must be within ${min}..${max} dB; got ${String(v)}`);
	return v;
}

//----- parse

/** True when `type` names one of the classic sampler unit commands this module parses. */
export function isClassicSamplerCommandType(type: unknown): type is ClassicSamplerCommand['type'] {
	return (CLASSIC_SAMPLER_COMMAND_TYPES as readonly unknown[]).includes(type);
}

/** Validate one sampler_unit_* wire record into a typed command; throws on any malformed field. */
export function parseClassicSamplerCommand(r: Rec): ClassicSamplerCommand {
	const type = r.type;
	if (type === 'sampler_unit_load') {
		_exact(r, ['type', 'bank', 'slot', 'stable_id', 'path', 'title', 'duration_ms']);
		if (r.stable_id !== undefined && r.path !== undefined)
			throw new TypeError('sampler_unit_load takes a stable_id or a path, not both');
		const out: ClassicSamplerCommand = { type, ..._address(r) };
		if (r.stable_id !== undefined) out.stable_id = _text('stable_id', r.stable_id);
		if (r.path !== undefined) out.path = _text('path', r.path);
		if (r.title !== undefined) out.title = _text('title', r.title);
		if (r.duration_ms !== undefined) {
			if (
				typeof r.duration_ms !== 'number' ||
				!Number.isFinite(r.duration_ms) ||
				r.duration_ms <= 0
			)
				throw new RangeError(`duration_ms must be a number > 0; got ${String(r.duration_ms)}`);
			out.duration_ms = r.duration_ms;
		}
		return out;
	}
	if (type === 'sampler_unit_trigger') {
		_exact(r, ['type', 'bank', 'slot', 'held']);
		if (r.held !== undefined && typeof r.held !== 'boolean')
			throw new TypeError('held must be boolean');
		return { type, ..._address(r), ...(r.held !== undefined ? { held: r.held as boolean } : {}) };
	}
	if (type === 'sampler_unit_stop') {
		_exact(r, ['type', 'bank', 'slot']);
		if (r.bank === undefined && r.slot === undefined) return { type };
		return { type, ..._address(r) };
	}
	if (type === 'sampler_unit_mode') {
		_exact(r, ['type', 'bank', 'slot', 'mode']);
		if (!(BANK_MODES as readonly unknown[]).includes(r.mode))
			throw new RangeError(`mode must be one of ${BANK_MODES.join('|')}; got ${String(r.mode)}`);
		return { type, ..._address(r), mode: r.mode as BankMode };
	}
	if (type === 'sampler_unit_gain') {
		_exact(r, ['type', 'bank', 'slot', 'gain_db']);
		return { type, ..._address(r), gain_db: _db('gain_db', r.gain_db, GAIN_DB_MIN, GAIN_DB_MAX) };
	}
	if (type === 'sampler_unit_master_gain') {
		_exact(r, ['type', 'gain_db']);
		return {
			type,
			gain_db: _db('gain_db', r.gain_db, MASTER_GAIN_DB_MIN, MASTER_GAIN_DB_MAX)
		};
	}
	throw new TypeError(`not a classic sampler command: ${String(type)}`);
}
