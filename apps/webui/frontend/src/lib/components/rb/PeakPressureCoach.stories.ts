import type { Meta, StoryObj } from '@storybook/sveltekit';

import PeakPressureCoach from './PeakPressureCoach.svelte';

/**
 * Educational energy-pressure cue from track metadata (not the room).
 * Renders nothing when the cue is `unknown` or the label is empty.
 */
const meta = {
	title: 'Components/PeakPressureCoach',
	component: PeakPressureCoach,
	tags: ['autodocs'],
	argTypes: {
		cue: { control: 'inline-radio', options: ['keep_building', 'hold', 'release', 'unknown'] }
	}
} satisfies Meta<typeof PeakPressureCoach>;

export default meta;
type Story = StoryObj<typeof meta>;

export const KeepBuilding: Story = { args: { cue: 'keep_building', cueLabel: 'Keep building' } };

export const Hold: Story = { args: { cue: 'hold', cueLabel: 'Hold the peak' } };

export const Release: Story = { args: { cue: 'release', cueLabel: 'Release pressure' } };

/** Unknown cue: the component renders nothing at all. */
export const UnknownRendersNothing: Story = {
	tags: ['renders-nothing'],
	args: { cue: 'unknown', cueLabel: '' }
};
