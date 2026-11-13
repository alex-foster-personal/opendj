/**
 * Beat FX ROLL (issue #9794): named in the unit and refused as not built. `roll-fx-kernel.ts`
 * is a 128-frame block core with no AudioWorklet host on main, so the unit's slot has no
 * Web Audio node to place yet; the effect lists as unbuilt until that host lands.
 */
import { parseRollDivision } from '../../roll-fx-kernel';
import { RecordingKernelHost } from './kernel-host';
import { registerBeatFxEffect } from './registry';

registerBeatFxEffect({
	name: 'roll',
	levelLaw: 'depth',
	builtOnMain: false,
	usesDivision: true,
	assertDivisionAllowed: (beats) => void parseRollDivision(beats),
	createHost: () => new RecordingKernelHost('roll')
});
