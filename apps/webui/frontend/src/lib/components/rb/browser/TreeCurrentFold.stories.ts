import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn } from 'storybook/test';

import { contracts } from './TreeCurrentFold.contract';
import TreeCurrentFold from './TreeCurrentFold.svelte';

const meta = {
	title: 'Contracts/Browser/TreeCurrentFold',
	component: TreeCurrentFold,
	tags: ['autodocs'],
	args: { onjump: fn() },
	parameters: { layout: 'padded' }
} satisfies Meta<typeof TreeCurrentFold>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Above: Story = contracts.Above;
export const Below: Story = contracts.Below;
export const InView: Story = contracts.InView;

/** Current playlist visible: renders nothing. */
export const Hidden: Story = {
	tags: ['renders-nothing'],
	args: { fold: null },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.current-fold')).toBeNull();
	}
};
