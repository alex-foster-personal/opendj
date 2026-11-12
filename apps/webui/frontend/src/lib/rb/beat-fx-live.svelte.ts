/**
 * Live beat FX unit state for the typed dispatcher and mirror (issues #9783, #10015).
 */
import { parseBeatFxCommand, type BeatFxCommand } from './beat-fx';
import {
	DEFAULT_BEAT_FX_UNIT,
	applyBeatFxUnitCommand,
	beatFxUnitView,
	type BeatFxUnitState,
	type BeatFxUnitView
} from './beat-fx-unit';

export const beatFxUnit = $state<BeatFxUnitState>({ ...DEFAULT_BEAT_FX_UNIT, host: null });

export function applyBeatFx(command: BeatFxCommand, at = 0): void {
	const parsed = parseBeatFxCommand({ ...command, type: 'beat_fx' });
	applyBeatFxUnitCommand(beatFxUnit, parsed, at);
}

export function beatFxView(): BeatFxUnitView {
	return beatFxUnitView(beatFxUnit);
}
