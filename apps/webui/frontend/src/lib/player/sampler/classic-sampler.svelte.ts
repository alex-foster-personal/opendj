/**
 * Classic sampler store (issue #9987): the 4 x 16 unit as $state, loaded from and saved to
 * `/api/v1/sampler/banks`, with triggers fired through the edge sampler's engine seam
 * ((private path) 2: one engine, two surfaces). Like the edge store it never imports the
 * engine: it plays through the one the edge overlay registers (sharedSamplerEngine); until then
 * NO_ENGINE refuses loudly.
 * #9989 adds play modes (press and release), slot gains and a master gain: mode and gain edits
 * save through one debounced PUT; the master gain is local (the banks document has no field for it).
 * Gains reach the voice as its level at trigger time; the seam has no live level call, so a gain
 * change on a sounding slot is heard from its next trigger. No UI and no bus commands yet.
 */

import { fetchSamplerBanks, putSamplerBanks, type BankMode } from './api-sampler-banks';
import type { FetchFn } from './api-sampler';
import {
	banksDocOf,
	emptyUnit,
	checkedMasterGain,
	loadSlot,
	pressAction,
	releaseAction,
	setSlotGain,
	setSlotMode,
	slotAt,
	slotTriggerPlan,
	sourceKey,
	unitFromBanksView,
	type ClassicUnit,
	type PressAction,
	type SlotAddress,
	type SlotLoad
} from './classic-sampler-logic';
import { sharedSamplerEngine, type SamplerEngine } from './sampler-engine-seam';

/** Mode and gain edits are saved this long after the last one (#9994: debounced 500 ms). */
export const BANKS_SAVE_DEBOUNCE_MS = 500;

export const classicSampler = $state({
	unit: emptyUnit() as ClassicUnit,
	loaded: false,
	master_gain_db: 0,
	/** the last debounced save's failure, null when it succeeded */
	save_error: null as string | null
});

let _fetch: FetchFn = (input, init) => fetch(input, init);
/** A test's engine in place of the shared one; null plays through the shared seam. */
let _engineOverride: SamplerEngine | null = null;
let _saveTimer: ReturnType<typeof setTimeout> | null = null;

/** Plug in the transport, or a test's engine in place of the shared one. */
export function setClassicSamplerPorts(ports: { fetchFn?: FetchFn; engine?: SamplerEngine }): void {
	if (ports.fetchFn !== undefined) _fetch = ports.fetchFn;
	if (ports.engine !== undefined) _engineOverride = ports.engine;
}

const _engine = (): SamplerEngine => _engineOverride ?? sharedSamplerEngine();

function _durations(unit: ClassicUnit): Map<string, number> {
	const out = new Map<string, number>();
	for (const s of unit.banks.flat())
		if (s?.duration_ms) out.set(sourceKey(s.source), s.duration_ms);
	return out;
}

/** Read the stored banks into the unit (once at boot), keeping lengths already known. */
export async function loadClassicBanks(): Promise<void> {
	const view = await fetchSamplerBanks(_fetch);
	classicSampler.unit = unitFromBanksView(view, _durations(classicSampler.unit));
	classicSampler.loaded = true;
}

/** Load (or clear) one slot and write the banks through; the server's answer becomes the unit. */
export async function loadClassicSlot(a: SlotAddress, load: SlotLoad): Promise<void> {
	const next = loadSlot(classicSampler.unit, a, load);
	const view = await putSamplerBanks(banksDocOf(next), _fetch);
	classicSampler.unit = unitFromBanksView(view, _durations(next));
}

//----- modes and gains (#9989)

function _scheduleSave(): void {
	if (_saveTimer !== null) clearTimeout(_saveTimer);
	_saveTimer = setTimeout(() => {
		flushClassicBanks().catch((e: unknown) => {
			classicSampler.save_error = e instanceof Error ? e.message : String(e);
		});
	}, BANKS_SAVE_DEBOUNCE_MS);
}

/** PUT the unit now if an edit is waiting for its debounced save (rejects on a refused save). */
export async function flushClassicBanks(): Promise<void> {
	if (_saveTimer === null) return;
	clearTimeout(_saveTimer);
	_saveTimer = null;
	await putSamplerBanks(banksDocOf(classicSampler.unit), _fetch);
	classicSampler.save_error = null;
}

/** Set a slot's play mode (from its next trigger) and save it debounced. */
export function setClassicSlotMode(a: SlotAddress, mode: BankMode): void {
	classicSampler.unit = setSlotMode(classicSampler.unit, a, mode);
	_scheduleSave();
}

/** Set a slot's gain (-24..+6 dB, from its next trigger) and save it debounced. */
export function setClassicSlotGain(a: SlotAddress, gain_db: number): void {
	classicSampler.unit = setSlotGain(classicSampler.unit, a, gain_db);
	_scheduleSave();
}

/** Set the sampler master gain (-60..0 dB, from each slot's next trigger); local only. */
export function setClassicMasterGain(gain_db: number): void {
	classicSampler.master_gain_db = checkedMasterGain(gain_db);
}

type PressResult = { ok: true; action: PressAction } | { ok: false; reason: string };

/** A pad press: start it, restart a sounding one shot or gate, or stop a sounding loop. */
export async function pressClassicSlot(a: SlotAddress): Promise<PressResult> {
	const plan = slotTriggerPlan(classicSampler.unit, a, classicSampler.master_gain_db);
	if (!plan.ok) return plan;
	const id = plan.engineSlot.slot_id;
	const action = pressAction(slotAt(classicSampler.unit, a)!.mode, _engine().phase(id) !== 'free');
	if (action !== 'start') _engine().stop(id);
	if (action !== 'stop') await _engine().toggle(plan.engineSlot);
	return { ok: true, action };
}

/** A pad release: a gate slot stops (its current voice, so a retriggered gate never hangs). */
export function releaseClassicSlot(a: SlotAddress): void {
	const s = slotAt(classicSampler.unit, a);
	if (s === null || releaseAction(s.mode) === 'none') return;
	stopClassicSlots(a);
}

/** Stop one slot, or every loaded slot when no address is given (`sampler_stop`). */
export function stopClassicSlots(a?: SlotAddress): void {
	const unit = classicSampler.unit;
	const addresses =
		a !== undefined
			? [a]
			: unit.banks.flatMap((bank, b) => bank.map((_, slot) => ({ bank: b, slot })));
	for (const addr of addresses) {
		if (slotAt(unit, addr) === null) continue;
		const plan = slotTriggerPlan(unit, addr);
		if (plan.ok) _engine().stop(plan.engineSlot.slot_id);
	}
}
