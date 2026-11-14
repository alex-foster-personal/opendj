/**
 * Classic sampler unit core (issue #9987, rekordbox parity): four banks of sixteen slots, pure.
 * (private path) section 2: one sampler engine, two surfaces. This module holds no audio;
 * a trigger becomes a SamplerSlot that the edge sampler's engine (voices 5..8, master bus)
 * plays through the same seam the edge panel uses, so there is never a second engine.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 4 x 16 slots, addressed by bank 0..3 and slot 0..15, labelled A1..D16
 *     [if] bank 4 or slot 16 is addressed [then] refused naming the bad index [else ⛔️]
 *   ✔︎ ✅ 🎯 a load names exactly one source (library track or file); neither clears the slot
 *     [if] both stable_id and path are given [then] refused naming bank and slot [else ⛔️]
 *   ✔︎ ✅ 🎯 the banks document round-trips (server shape, sampler_banks.py)
 *     [if] a GET view is read then written back [then] the PUT document holds the same slots [else ⛔️]
 *   ✔︎ ✅ 🎯 a trigger plays the whole source once from the start at unity, on the master
 *     [if] a loaded slot fires [then] one shot, launch off, own tempo, level 100, region 0..duration [else ⛔️]
 *     [if] an empty, missing or undecoded slot fires [then] a reason naming bank and slot [else ⛔️]
 *   ✔︎ ✅ 🎯 play modes (#9989, rekordbox parity, mimicked): one shot, loop, gate
 *     [if] a one shot or gate pad is pressed while it sounds [then] it restarts [else ⛔️]
 *     [if] a loop pad is pressed while it sounds [then] it stops; else it loops the whole source [else ⛔️]
 *     [if] a gate pad is released [then] it stops; a one shot or loop release does nothing [else ⛔️]
 *   ✔︎ ✅ 🎯 slot gain -24..+6 dB and master gain -60..0 dB combine into the voice level
 *     [if] slot -6 dB and master -6 dB [then] level 100 x 10^(-12/20) [else ⛔️]
 *     [if] the combined gain is above 0 dB [then] the level holds at 100 (the voice cannot boost) [else ⛔️]
 *     [if] a gain is outside its range or a mode is unknown [then] refused naming it [else ⛔️]
 */

import type { BankMode, BankSlot, BanksDoc, BanksView } from './api-sampler-banks';
import type { SamplerSlot, SlotSettings } from './sampler-types';

export const BANK_COUNT = 4;
export const SLOTS_PER_BANK = 16;
export const BANK_MODES: readonly BankMode[] = ['one_shot', 'loop', 'gate'];
export const GAIN_DB_MIN = -24;
export const GAIN_DB_MAX = 6;
/** The sampler deck's master gain range (#9989 acceptance); local, not in the banks document. */
export const MASTER_GAIN_DB_MIN = -60;
export const MASTER_GAIN_DB_MAX = 0;
const BANK_LETTERS = 'ABCD';

export type ClassicSource = { kind: 'track'; stable_id: string } | { kind: 'file'; path: string };

export interface ClassicSlot {
	source: ClassicSource;
	title: string;
	mode: BankMode;
	gain_db: number;
	/** whole-source length; null until the library or a decode tells it */
	duration_ms: number | null;
	/** the track no longer resolves in the library (GET /banks `missing`) */
	missing: boolean;
}

export interface ClassicUnit {
	active_bank: number;
	banks: Array<Array<ClassicSlot | null>>;
}

export interface SlotAddress {
	bank: number;
	slot: number;
}

/** `sampler_load` body: exactly one of stable_id or path fills the slot, neither clears it. */
export interface SlotLoad {
	stable_id?: string;
	path?: string;
	title?: string;
	duration_ms?: number | null;
}

export type TriggerPlan =
	{ ok: true; address: SlotAddress; engineSlot: SamplerSlot } | { ok: false; reason: string };

/** A refused classic sampler call; the message names the bank and slot when there is one. */
export class ClassicSamplerError extends Error {
	constructor(message: string) {
		super(message);
		this.name = 'ClassicSamplerError';
	}
}

//----- addressing

/** A checked slot address: whole numbers, bank 0..3, slot 0..15. */
export function slotAddress(bank: number, slot: number): SlotAddress {
	if (!Number.isInteger(bank) || bank < 0 || bank >= BANK_COUNT)
		throw new ClassicSamplerError(`sampler bank must be 0..${BANK_COUNT - 1}, got ${bank}`);
	if (!Number.isInteger(slot) || slot < 0 || slot >= SLOTS_PER_BANK)
		throw new ClassicSamplerError(`sampler slot must be 0..${SLOTS_PER_BANK - 1}, got ${slot}`);
	return { bank, slot };
}

/** The pad label rekordbox shows: bank letter and 1-based slot, e.g. `B3`. */
export function slotLabel(a: SlotAddress): string {
	return `${BANK_LETTERS[a.bank]}${a.slot + 1}`;
}

const _where = (a: SlotAddress): string => `bank ${BANK_LETTERS[a.bank]} slot ${a.slot + 1}`;

/** The unit a fresh launch starts with: bank A active, every slot empty. */
export function emptyUnit(): ClassicUnit {
	return {
		active_bank: 0,
		banks: Array.from({ length: BANK_COUNT }, () =>
			Array<ClassicSlot | null>(SLOTS_PER_BANK).fill(null)
		)
	};
}

/** The slot at an address, null when empty. */
export function slotAt(unit: ClassicUnit, a: SlotAddress): ClassicSlot | null {
	return unit.banks[a.bank][a.slot];
}

function _withSlot(unit: ClassicUnit, a: SlotAddress, slot: ClassicSlot | null): ClassicUnit {
	const banks = unit.banks.map((b, i) =>
		i === a.bank ? b.map((s, j) => (j === a.slot ? slot : s)) : b
	);
	return { ...unit, banks };
}

//----- loading

/** Fill a slot from one source (one shot, 0 dB), or clear it when the load names none. */
export function loadSlot(unit: ClassicUnit, a: SlotAddress, load: SlotLoad): ClassicUnit {
	const { stable_id, path } = load;
	if (stable_id !== undefined && path !== undefined)
		throw new ClassicSamplerError(`${_where(a)}: load a library track or a file, not both`);
	if (stable_id === undefined && path === undefined) return _withSlot(unit, a, null);
	const source: ClassicSource =
		stable_id !== undefined ? { kind: 'track', stable_id } : { kind: 'file', path: path! };
	const id = source.kind === 'track' ? source.stable_id : source.path;
	if (id === '') throw new ClassicSamplerError(`${_where(a)}: the ${source.kind} source is empty`);
	const duration = load.duration_ms ?? null;
	if (duration !== null && !(Number.isFinite(duration) && duration > 0))
		throw new ClassicSamplerError(`${_where(a)}: duration_ms must be > 0, got ${duration}`);
	const title = load.title ?? (source.kind === 'file' ? source.path.split(/[\\/]/).pop()! : id);
	return _withSlot(unit, a, {
		source,
		title,
		mode: 'one_shot',
		gain_db: 0,
		duration_ms: duration,
		missing: false
	});
}

/** Switch the active bank (`sampler_bank`). */
export function selectBank(unit: ClassicUnit, bank: number): ClassicUnit {
	return { ...unit, active_bank: slotAddress(bank, 0).bank };
}

//----- the banks document (GET / PUT /api/v1/sampler/banks)

function _checkedSlot(s: Pick<BankSlot, 'mode' | 'gain_db'>, a: SlotAddress): void {
	if (!BANK_MODES.includes(s.mode))
		throw new ClassicSamplerError(`${_where(a)}: unknown mode ${s.mode}`);
	if (!(s.gain_db >= GAIN_DB_MIN && s.gain_db <= GAIN_DB_MAX))
		throw new ClassicSamplerError(
			`${_where(a)}: gain ${s.gain_db} dB is outside ${GAIN_DB_MIN}..+${GAIN_DB_MAX}`
		);
}

/** The unit a GET view describes; `durations` keeps lengths already known per source key. */
export function unitFromBanksView(
	view: BanksView,
	durations: ReadonlyMap<string, number> = new Map()
): ClassicUnit {
	if (view.v !== 1) throw new ClassicSamplerError(`unsupported banks document version ${view.v}`);
	if (view.banks.length !== BANK_COUNT || view.banks.some((b) => b.length !== SLOTS_PER_BANK))
		throw new ClassicSamplerError(
			`a banks document is ${BANK_COUNT} banks of ${SLOTS_PER_BANK} slots`
		);
	const banks = view.banks.map((bank, b) =>
		bank.map((s, i): ClassicSlot | null => {
			if (s === null) return null;
			_checkedSlot(s, { bank: b, slot: i });
			const source: ClassicSource =
				s.stable_id !== undefined
					? { kind: 'track', stable_id: s.stable_id }
					: { kind: 'file', path: s.path! };
			const duration = durations.get(sourceKey(source)) ?? null;
			return {
				source,
				title: s.title,
				mode: s.mode,
				gain_db: s.gain_db,
				duration_ms: duration,
				missing: s.missing
			};
		})
	);
	return { active_bank: slotAddress(view.active_bank, 0).bank, banks };
}

/** The PUT document for a unit: sources, modes, gains and titles (lengths and `missing` stay local). */
export function banksDocOf(unit: ClassicUnit): BanksDoc {
	const banks = unit.banks.map((bank, b) =>
		bank.map((s, i): BankSlot | null => {
			if (s === null) return null;
			_checkedSlot(s, { bank: b, slot: i });
			const common = { mode: s.mode, gain_db: s.gain_db, title: s.title };
			return s.source.kind === 'track'
				? { stable_id: s.source.stable_id, ...common }
				: { path: s.source.path, ...common };
		})
	);
	return { v: 1, active_bank: unit.active_bank, banks };
}

/** `track:<stable_id>` or `file:<path>`: one key per source, for cached lengths and clips. */
export function sourceKey(source: ClassicSource): string {
	return source.kind === 'track' ? `track:${source.stable_id}` : `file:${source.path}`;
}

//----- modes and gains (#9989)

/** Set a loaded slot's play mode; it takes effect on the slot's next trigger. */
export function setSlotMode(unit: ClassicUnit, a: SlotAddress, mode: BankMode): ClassicUnit {
	const s = _loaded(unit, a);
	const next = { ...s, mode };
	_checkedSlot(next, a);
	return _withSlot(unit, a, next);
}

/** Set a loaded slot's gain, -24..+6 dB. */
export function setSlotGain(unit: ClassicUnit, a: SlotAddress, gain_db: number): ClassicUnit {
	const s = _loaded(unit, a);
	const next = { ...s, gain_db };
	_checkedSlot(next, a);
	return _withSlot(unit, a, next);
}

/** A checked sampler master gain, -60..0 dB. */
export function checkedMasterGain(gain_db: number): number {
	if (!(gain_db >= MASTER_GAIN_DB_MIN && gain_db <= MASTER_GAIN_DB_MAX))
		throw new ClassicSamplerError(
			`sampler master gain ${gain_db} dB is outside ${MASTER_GAIN_DB_MIN}..${MASTER_GAIN_DB_MAX}`
		);
	return gain_db;
}

function _loaded(unit: ClassicUnit, a: SlotAddress): ClassicSlot {
	const s = slotAt(unit, a);
	if (s === null) throw new ClassicSamplerError(`${_where(a)} is empty`);
	return s;
}

/** Decibels to a linear amplitude factor. */
export function dbToLinear(db: number): number {
	return 10 ** (db / 20);
}

/** The engine voice level 0..100 for slot plus master gain; the voice cannot boost, so it caps at 100. */
export function voiceLevel(slotGainDb: number, masterGainDb: number): number {
	return Math.min(100, 100 * dbToLinear(slotGainDb + masterGainDb));
}

/** What a pad press does: start a silent slot; a sounding loop stops; a sounding one shot or gate restarts. */
export type PressAction = 'start' | 'restart' | 'stop';

/** The press transition for a slot's mode and whether it sounds now. */
export function pressAction(mode: BankMode, sounding: boolean): PressAction {
	if (!sounding) return 'start';
	if (mode === 'loop') return 'stop';
	return 'restart';
}

/** The release transition: only a gate pad stops when let go. */
export function releaseAction(mode: BankMode): 'stop' | 'none' {
	return mode === 'gate' ? 'stop' : 'none';
}

//----- trigger

/**
 * The engine settings for a mode at a level: no quantise, own tempo, no key shift. Loop repeats the
 * whole region; one shot and gate play it once (gate is stopped by the release).
 */
export function slotSettings(mode: BankMode, level: number): SlotSettings {
	return {
		launch: 'off',
		key: 'off',
		tempo: 'own',
		mode,
		repeat: mode === 'loop' ? 'loop' : 'once',
		downbeat_offset: 0,
		level
	};
}

/** The engine slot id of a classic slot: its pad and its source, so a reload never reuses a stale clip. */
export function engineSlotId(a: SlotAddress, source: ClassicSource): string {
	return `classic:${slotLabel(a)}:${encodeURIComponent(sourceKey(source))}`;
}

/**
 * What `sampler_trigger {bank, slot}` plays: the whole source in the slot's mode at its gain under
 * the master gain, or why it cannot. `beats: 1` makes the whole region one loop pass for the engine.
 */
export function slotTriggerPlan(unit: ClassicUnit, a: SlotAddress, masterGainDb = 0): TriggerPlan {
	const s = slotAt(unit, a);
	if (s === null) return { ok: false, reason: `${_where(a)} is empty` };
	if (s.missing) return { ok: false, reason: `${_where(a)}: its track is gone from the library` };
	if (s.duration_ms === null)
		return { ok: false, reason: `${_where(a)}: its length is not known yet` };
	const engineSlot: SamplerSlot = {
		slot_id: engineSlotId(a, s.source),
		stable_id: s.source.kind === 'track' ? s.source.stable_id : '',
		source: 'capture',
		cue_letter: null,
		in_ms: 0,
		out_ms: s.duration_ms,
		beats: 1,
		native_bpm: 0,
		key: null,
		energy: null,
		color: '',
		title: s.title,
		artist: '',
		name: null,
		description: '',
		vector: null,
		settings: slotSettings(s.mode, voiceLevel(s.gain_db, checkedMasterGain(masterGainDb))),
		flags: { broken: false, remix: false, vocals: false, offline: false },
		peaks: { low: [], mid: [], high: [] },
		stems_ready: false,
		stem_parts: null
	};
	return { ok: true, address: a, engineSlot };
}
