import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import SettingNumberStepper from './SettingNumberStepper.svelte';

/**
 * A number setting's stepper row: minus and plus nudge by one step (Shift for
 * ten), the readout is typed into directly, and out-of-range text is refused
 * rather than clamped. The host owns the value and applies the change.
 */
const meta = {
	title: 'Components/Settings/SettingNumberStepper',
	component: SettingNumberStepper,
	tags: ['autodocs'],
	args: {
		id: 'setting-wave-zoom',
		label: 'Waveform zoom',
		title: 'How many bars the overview shows',
		control: { kind: 'number', min: 1, max: 64, step: 1, defaultValue: 16, unit: ' bars' },
		value: 16,
		onchange: fn()
	}
} satisfies Meta<typeof SettingNumberStepper>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Mid-range: both nudges available. */
export const MidRange: Story = {};

/** At the maximum: plus has nothing to do and returns null to the host. */
export const AtMax: Story = { args: { value: 64 } };

/** A fractional step formats the readout with two decimals. */
export const FractionalStep: Story = {
	args: {
		id: 'setting-gain-trim',
		label: 'Gain trim',
		title: 'Trim applied before the channel fader',
		control: { kind: 'number', min: -6, max: 6, step: 0.25, defaultValue: 0, unit: ' dB' },
		value: 1.5
	}
};
