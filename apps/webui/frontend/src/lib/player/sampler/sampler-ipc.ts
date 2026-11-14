/**
 * Performance-bus handlers for the edge sampler's commands
 * ((private path) section 10, agent parity; #10786).
 *
 * Requirements (mini-PRD):
 *   ✔︎ each sampler_* command calls the same store action the UI calls (sampler-store.svelte.ts)
 *   ✔︎ a slot_id or ref that names no slot throws, so the bus reports the error
 *     [if] sampler_trigger ref S9-99 names no slot [then ⛔️ the bus sees an error, not a no-op]
 *   ✔︎ a play or preview with no audio engine registered throws SamplerNoEngineError
 *     [if] sampler_trigger runs before the engine registers [then ⛔️ the bus sees the error]
 *   ✔︎ sampler_stop {all} stops every armed or playing slot; sampler_preview {stop} stops the preview
 *   ✔︎ sampler_release lets go of a press (releaseSlot): a gate slot stops, loop and one-shot ignore it
 *     [if] a gate slot's key is released while its clip still loads [then] it stops once it starts [else ⛔️]
 *   ✔︎ sampler_slot_update merges a partial settings patch over the slot's own settings (updateSlot)
 *     [if] settings {level: 50} is sent [then] the PATCH carries every other setting unchanged [else ⛔️]
 *   ✔︎ sampler_capture runs captureToSet, the store action a deck loop drop runs (#11127)
 *     [if] sampler_capture names a set index the edges do not have [then ⛔️ the bus sees an error]
 *     [if] the deck has no engaged loop [then ⛔️ the bus sees an error, nothing is posted]
 *   ✔︎ delete calls the client API and reloads the store: the store has no deleteSlot action yet
 *     [if] sampler_delete names a cue slot [then ⛔️ the server's 409 reaches the bus]
 *   → engine rejections after registration are toasted by the store wrappers, not rethrown
 */

import { deleteSamplerSlot } from './api-sampler';
import { SamplerNoEngineError } from './sampler-engine-seam';
import type { SamplerCommand } from './sampler-ipc-guards';
import {
	captureToSet,
	clearEdgeResults,
	fireSlot,
	flipSet,
	loadSampler,
	nameSet,
	openSlotEditor,
	pinSlot,
	releaseSlot,
	resolveRef,
	sampler,
	setEdgeFilter,
	setEdgeSearch,
	setEdgeSet,
	setSamplerEnabled,
	showEdgeResults,
	slotPhase,
	stopSampleSlot,
	togglePreview,
	updateSlot
} from './sampler-store.svelte';
import type { SamplerSlot } from './sampler-types';

//----- helpers

function _slot(address: { slot_id?: string; ref?: string }): SamplerSlot {
	if (address.slot_id !== undefined) {
		const slot = sampler.slots.find((s) => s.slot_id === address.slot_id);
		if (slot === undefined) throw new Error(`sampler: no slot ${address.slot_id}`);
		return slot;
	}
	if (address.ref === undefined) throw new Error('sampler: a slot_id or ref is required');
	const slot = resolveRef(address.ref);
	if (slot === null) throw new Error(`sampler: ref ${address.ref} names no slot on the edges`);
	return slot;
}

function _requireEngine(what: string): void {
	if (!sampler.engineReady) throw new SamplerNoEngineError(what);
}

function _stopAll(): void {
	for (const s of sampler.slots) {
		const phase = slotPhase(s.slot_id);
		if (phase === 'armed' || phase === 'playing') stopSampleSlot(s.slot_id);
	}
}

async function _stopPreview(): Promise<void> {
	const previewing = sampler.slots.find((s) => slotPhase(s.slot_id) === 'previewing');
	if (previewing !== undefined) await togglePreview(previewing, true);
}

//----- dispatch

/** Run one sampler bus command through the store action its UI control uses. */
export async function applySamplerCommand(command: SamplerCommand): Promise<void> {
	switch (command.type) {
		case 'sampler_enabled':
			setSamplerEnabled(command.on);
			return;
		case 'sampler_trigger': {
			const slot = _slot(command);
			_requireEngine(`play ${slot.slot_id}`);
			await fireSlot(slot);
			return;
		}
		case 'sampler_stop':
			if (command.all === true) _stopAll();
			else stopSampleSlot(_slot(command).slot_id);
			return;
		case 'sampler_release':
			releaseSlot(_slot(command).slot_id);
			return;
		case 'sampler_preview': {
			if (command.stop === true) {
				await _stopPreview();
				return;
			}
			const slot = _slot(command);
			_requireEngine(`preview ${slot.slot_id}`);
			await togglePreview(slot, false);
			return;
		}
		case 'sampler_set_page':
			if (command.set !== undefined) setEdgeSet(command.edge, command.set);
			else if (command.delta !== undefined) flipSet(command.edge, command.delta);
			else throw new Error('sampler_set_page needs set or delta');
			return;
		case 'sampler_pin':
			await pinSlot(_slot(command).slot_id, command.pinned);
			return;
		case 'sampler_search':
			setEdgeSearch(command.edge, command.query);
			return;
		case 'sampler_filter':
			setEdgeFilter(command.edge, command.filter, command.on);
			return;
		case 'sampler_results':
			if (command.show) showEdgeResults(command.edge);
			else clearEdgeResults(command.edge);
			return;
		case 'sampler_slot_update': {
			const slot = _slot(command);
			const { name, description, settings } = command;
			await updateSlot(slot.slot_id, {
				...(name !== undefined ? { name } : {}),
				...(description !== undefined ? { description } : {}),
				...(settings !== undefined ? { settings: { ...slot.settings, ...settings } } : {})
			});
			return;
		}
		case 'sampler_set_name':
			await nameSet(command.set_id, command.name, command.description);
			return;
		case 'sampler_capture':
			await captureToSet(command.deck, command.set, command.index);
			return;
		case 'sampler_edit':
			openSlotEditor(command.close === true ? null : _slot(command).slot_id);
			return;
		case 'sampler_delete':
			await deleteSamplerSlot(_slot(command).slot_id);
			await loadSampler();
			return;
		default: {
			const _exhaustive: never = command;
			throw new Error(`Unhandled sampler command: ${String(_exhaustive)}`);
		}
	}
}
