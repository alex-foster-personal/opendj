/**
 * Registers every row of the time-based catalog as a beat FX effect (issue #13726).
 * Rows with `builtOnMain: false` register too, so the unit can refuse them by name.
 * Built echo rows carry the shared echo kernel for the unit's slot (issue #9782), each in the
 * kernel mode its name gives (issue #13843).
 */
import type { EchoMode } from '../echo-kernel';
import { TIME_FX_CATALOG, assertTimeFxDivisionAllowed } from '../time-fx-catalog';
import { createEchoSlotKernel, echoSlotMode } from './echo-slot-kernel';
import { RecordingKernelHost, type BeatFxKernel } from './kernel-host';
import { registerBeatFxEffect } from './registry';

/** The slot kernel factory for a built echo row; the mode is checked once at registration. */
function _echoSlot(mode: EchoMode): { createKernel(ctx: BaseAudioContext): BeatFxKernel } {
	return { createKernel: (ctx) => createEchoSlotKernel(ctx, mode) };
}

for (const row of TIME_FX_CATALOG) {
	registerBeatFxEffect({
		name: row.name,
		levelLaw: row.levelLaw,
		builtOnMain: row.builtOnMain,
		usesDivision: true,
		assertDivisionAllowed: (beats, bpm) => assertTimeFxDivisionAllowed(row.name, beats, bpm),
		createHost: () => new RecordingKernelHost(row.name),
		...(row.builtOnMain && row.kernel === 'echo' ? _echoSlot(echoSlotMode(row.name)) : {})
	});
}
