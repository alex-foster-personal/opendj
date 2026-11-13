/**
 * Selector node ids for the mixer's headphone cluster (#14334, epic #9372).
 *
 * `mixer.headphones` is a group holding one leaf per control: the MIX and LEVEL knobs, the
 * SHOW AUDIO I/O button and the Open MIDI panel button. Each leaf wraps exactly one control,
 * so Enter on a leaf presses that control and no other. The knob leaves also name their knob
 * registry id (`data-sel-knob`), so the arrows turn that knob. Ids compose under the existing
 * headphones id, so selector-register.ts needs no new builder.
 */
import { selId } from '$lib/keyboard/selector-register';
import { knobId } from '$lib/rb/knob-control.svelte';

export const HEADPHONE_SEL_CONTROLS = ['mix', 'level', 'io', 'midi'] as const;
export type HeadphoneSelControl = (typeof HEADPHONE_SEL_CONTROLS)[number];

/** Leaf id of one headphone control under the mixer's headphones group. */
export function headphoneSelId(control: HeadphoneSelControl): string {
	return `${selId.headphones()}.${control}`;
}

/** Knob registry ids the MIX and LEVEL leaves carry as `data-sel-knob`. */
export const HEADPHONE_SEL_KNOBS = {
	mix: knobId('hp', 'hp-mix'),
	level: knobId('hp', 'hp-level')
} as const;
