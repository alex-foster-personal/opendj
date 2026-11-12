/**
 * Pad FX controller, pure (PADFX-11718): turns pad press, release and arrow
 * steps into the exact `beat_fx` / `beat_fx_release` command sequence for the
 * one channel-select beat FX unit. No DOM, no runes.
 *
 * Mimicked: rekordbox 7.2.8 manual pp. 182-183. One unit, so the last pressed
 * pad wins; a Release FX pad turns the other Pad FX off; [<]/[>] while holding
 * changes the beats temporarily and release restores the preset's value.
 */
import {
	parseBeatFxCommand,
	parseBeatFxReleaseCommand,
	type BeatFxBeats,
	type BeatFxCommand,
	type BeatFxDeckTarget,
	type BeatFxEffect,
	type BeatFxReleaseCommand
} from './beat-fx';
import {
	PAD_FX_BANK_SIZE,
	beatFxDivisionsFor,
	padFxPresetReadiness,
	type PadFxPreset
} from './pad-fx-presets';

export type PadFxCommand = BeatFxCommand | BeatFxReleaseCommand;
export interface PadFxControllerState {
	held: readonly number[];
	active: number | null;
	releaseLatched: boolean;
	beatsOverride: BeatFxBeats | null;
}
export interface PadFxController {
	press(pad: number): PadFxCommand[];
	release(pad: number): PadFxCommand[];
	step(direction: -1 | 1): PadFxCommand[];
	releaseAll(): PadFxCommand[];
	state(): PadFxControllerState;
}

//-----------------------------------------------------------------------------
// helpers

function _checked(commands: Record<string, unknown>[]): PadFxCommand[] {
	return commands.map((c) =>
		c.type === 'beat_fx' ? parseBeatFxCommand(c) : parseBeatFxReleaseCommand(c)
	);
}

function _liveBeats(preset: PadFxPreset): { effect: BeatFxEffect; beats: BeatFxBeats } {
	if (preset.kind !== 'beat' || !('beats' in preset))
		throw new TypeError(`${preset.label} is not a beats preset`);
	return { effect: preset.effect as BeatFxEffect, beats: preset.beats };
}

//-----------------------------------------------------------------------------
// controller

export function createPadFxController(
	bank: readonly PadFxPreset[],
	deck: BeatFxDeckTarget
): PadFxController {
	if (bank.length !== PAD_FX_BANK_SIZE)
		throw new RangeError(`pad fx bank must have ${PAD_FX_BANK_SIZE} presets; got ${bank.length}`);
	let held: number[] = [];
	let active: number | null = null;
	let releaseLatched = false;
	let beatsOverride: BeatFxBeats | null = null;

	const off = (): Record<string, unknown> => ({ type: 'beat_fx', target: deck, enabled: false });
	const unlatch = (): Record<string, unknown> => ({ type: 'beat_fx_release', held: false });

	function _presetAt(pad: number): PadFxPreset {
		if (!Number.isInteger(pad) || pad < 0 || pad >= PAD_FX_BANK_SIZE)
			throw new RangeError(`pad ${pad} is outside 0..${PAD_FX_BANK_SIZE - 1}`);
		return bank[pad];
	}

	function press(pad: number): PadFxCommand[] {
		const preset = _presetAt(pad);
		if (held.includes(pad)) throw new Error(`pad ${pad} (${preset.label}) is already held`);
		const readiness = padFxPresetReadiness(preset);
		if (!readiness.live) throw new Error(`pad ${pad}: ${readiness.reason}`);
		const out: Record<string, unknown>[] = [];
		if (preset.kind === 'beat') {
			const { effect, beats } = _liveBeats(preset);
			if (releaseLatched) {
				out.push(unlatch());
				releaseLatched = false;
			}
			out.push({ type: 'beat_fx', target: deck, effect, beats, enabled: true });
			active = pad;
			beatsOverride = null;
		} else if (preset.kind === 'release') {
			out.push(off());
			active = null;
			beatsOverride = null;
			const engage = {
				type: 'beat_fx_release',
				held: true,
				beats: preset.beats,
				kind: preset.release
			};
			if (preset.hold) out.push(engage);
			else if (releaseLatched) {
				out.push(unlatch());
				releaseLatched = false;
			} else {
				out.push(engage);
				releaseLatched = true;
			}
		}
		held.push(pad);
		return _checked(out);
	}

	function release(pad: number): PadFxCommand[] {
		const preset = _presetAt(pad);
		if (!held.includes(pad)) throw new Error(`pad ${pad} (${preset.label}) is not held`);
		held = held.filter((p) => p !== pad);
		if (pad === active) {
			active = null;
			beatsOverride = null;
			return _checked([off()]);
		} else if (preset.kind === 'release' && preset.hold) {
			return _checked([unlatch()]);
		}
		return [];
	}

	function step(direction: -1 | 1): PadFxCommand[] {
		if (active === null) return [];
		const { effect, beats } = _liveBeats(bank[active]);
		const divisions = beatFxDivisionsFor(effect);
		const next = divisions.indexOf(beatsOverride ?? beats) + direction;
		if (next < 0 || next >= divisions.length) return [];
		beatsOverride = divisions[next];
		return _checked([{ type: 'beat_fx', target: deck, beats: beatsOverride }]);
	}

	function releaseAll(): PadFxCommand[] {
		const out: PadFxCommand[] = [];
		for (const pad of [...held].sort((a, b) => a - b)) out.push(...release(pad));
		if (releaseLatched) {
			releaseLatched = false;
			out.push(..._checked([unlatch()]));
		}
		return out;
	}

	return {
		press,
		release,
		step,
		releaseAll,
		state: () => ({ held: [...held], active, releaseLatched, beatsOverride })
	};
}
