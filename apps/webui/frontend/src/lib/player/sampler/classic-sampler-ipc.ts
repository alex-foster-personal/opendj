/**
 * Performance-bus handlers for the classic sampler unit's commands (#9987 and #9989, agent parity).
 *
 * Requirements (mini-PRD):
 *   ✔︎ each sampler_unit_* command calls the classic store action a pad or slot control calls
 *     (classic-sampler.svelte.ts): one code path for UI and agents
 *   ✔︎ the stored banks are read before the first command touches the unit, so a load or a gain
 *     edit never writes an empty unit over them
 *     [if] sampler_unit_load runs before the banks were read [then ⛔️ the PUT drops the stored slots]
 *   ✔︎ a press the store refuses (empty slot) throws its reason, so the bus reports it
 *     [if] sampler_unit_trigger names an empty slot [then ⛔️ the bus sees no error]
 *   ✔︎ held:false is the pad release: a gate slot stops, one shot and loop ignore it
 *   → the mirror of the unit's state is not part of this module
 */

import {
	classicSampler,
	loadClassicBanks,
	loadClassicSlot,
	pressClassicSlot,
	releaseClassicSlot,
	setClassicMasterGain,
	setClassicSlotGain,
	setClassicSlotMode,
	stopClassicSlots
} from './classic-sampler.svelte';
import type { ClassicSamplerCommand } from './classic-sampler-ipc-guards';

async function _ensureBanksRead(): Promise<void> {
	if (!classicSampler.loaded) await loadClassicBanks();
}

/** Run one classic sampler bus command through the store action its pad or control uses. */
export async function applyClassicSamplerCommand(command: ClassicSamplerCommand): Promise<void> {
	if (command.type === 'sampler_unit_master_gain') {
		setClassicMasterGain(command.gain_db);
		return;
	}
	await _ensureBanksRead();
	switch (command.type) {
		case 'sampler_unit_load': {
			const { bank, slot, ...load } = command;
			await loadClassicSlot({ bank, slot }, load);
			return;
		}
		case 'sampler_unit_trigger': {
			const a = { bank: command.bank, slot: command.slot };
			if (command.held === false) {
				releaseClassicSlot(a);
				return;
			}
			const result = await pressClassicSlot(a);
			if (!result.ok) throw new Error(result.reason);
			return;
		}
		case 'sampler_unit_stop':
			stopClassicSlots(
				command.bank !== undefined && command.slot !== undefined
					? { bank: command.bank, slot: command.slot }
					: undefined
			);
			return;
		case 'sampler_unit_mode':
			setClassicSlotMode({ bank: command.bank, slot: command.slot }, command.mode);
			return;
		case 'sampler_unit_gain':
			setClassicSlotGain({ bank: command.bank, slot: command.slot }, command.gain_db);
			return;
		default: {
			const _exhaustive: never = command;
			throw new Error(`Unhandled classic sampler command: ${String(_exhaustive)}`);
		}
	}
}
