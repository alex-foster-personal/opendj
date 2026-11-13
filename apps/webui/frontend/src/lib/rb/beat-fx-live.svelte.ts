/**
 * Live beat FX unit state for the typed dispatcher and mirror (issues #9783, #10015); with
 * `fx.beat_rack` on, every command also drives the unit graph (issue #9782). With it off the
 * dispatcher refuses every beat FX command (issue #9783).
 */
import {
	parseBeatFxCommand,
	parseBeatFxReleaseCommand,
	type BeatFxCommand,
	type BeatFxReleaseCommand
} from './beat-fx';
import { beatFxRackEnabled } from './beat-fx-flag';
import { liveBeatFxInsert, syncBeatFxInsert } from './beat-fx-insert';
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
	const insert = liveBeatFxInsert();
	if (insert !== null) syncBeatFxInsert(insert, beatFxUnit, at);
}

/** The reason every beat FX command is refused while `fx.beat_rack` is off. */
export const BEAT_FX_RACK_OFF_REFUSAL = 'fx.beat_rack is off';

/** Refuse a beat FX command, changing nothing, unless `fx.beat_rack` is on. */
export function assertBeatFxRackOn(): void {
	if (!beatFxRackEnabled()) throw new Error(BEAT_FX_RACK_OFF_REFUSAL);
}

/** Hold or let go of Release FX; holding twice or letting go unheld is refused and changes nothing. */
export function applyBeatFxRelease(command: BeatFxReleaseCommand): void {
	const parsed = parseBeatFxReleaseCommand({ ...command, type: 'beat_fx_release' });
	if (parsed.held === beatFxUnit.release_held)
		throw new Error(`beat_fx_release is ${parsed.held ? 'already held' : 'not held'}`);
	beatFxUnit.release_held = parsed.held;
	// REVERB (issue #9821): holding Release FX cuts the wet over 10 ms and clears the tail.
	const insert = liveBeatFxInsert();
	if (parsed.held && insert !== null) insert.slot?.kernel.cut?.(insert.ctx.currentTime);
}

export function beatFxView(): BeatFxUnitView {
	return { ...beatFxUnitView(beatFxUnit), available: beatFxRackEnabled() };
}
