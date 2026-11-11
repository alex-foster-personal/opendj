import type { Meta, StoryObj } from '@storybook/sveltekit';

import EdgeWaveTopTab from './EdgeWaveTopTab.svelte';

const meta = {
	title: 'Components/Sampler/EdgeWaveTopTab',
	component: EdgeWaveTopTab,
	tags: ['autodocs'],
	parameters: { layout: 'centered' },
	args: {
		edge: 'L' as const,
		page: 0
	}
} satisfies Meta<typeof EdgeWaveTopTab>;

export default meta;
type Story = StoryObj<typeof meta>;

export const LeftSetOne: Story = {};

export const RightSetTwo: Story = {
	args: { edge: 'R', page: 1 }
};
