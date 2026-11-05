import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import SetTargetReadout from './SetTargetReadout.svelte';

/**
 * The set header readout (LIBUX-8679c): estimated played length of the
 * non-skipped tracks against the set's target. Click to edit the target in
 * minutes. Track counts and ids are invented.
 */
const summary = {
	playlist_id: 'pl-friday',
	played_s: 48 * 60,
	target_s: 3600,
	over_s: -12 * 60,
	skipped: 2,
	skipped_ids: [],
	clashes: [],
	readout: '~48m of 60m \u00b7 12m left'
};

const meta = {
	title: 'Components/Browser/SetTargetReadout',
	component: SetTargetReadout,
	tags: ['autodocs'],
	args: { summary, onsettarget: fn() }
} satisfies Meta<typeof SetTargetReadout>;

export default meta;
type Story = StoryObj<typeof meta>;

export const UnderTarget: Story = {};

export const OverTarget: Story = {
	args: { summary: { ...summary, played_s: 67 * 60, over_s: 7 * 60, readout: '~67m of 60m \u00b7 7m over' } }
};

export const NoTarget: Story = {
	args: { summary: { ...summary, target_s: null, over_s: null, readout: '~48m' } }
};
