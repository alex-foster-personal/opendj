import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn, within } from 'storybook/test';

import { contracts } from './AssignMatrix.contract';
import AssignMatrix from './AssignMatrix.svelte';

/**
 * 2x2 crossfader assign grid (1 2 / 3 4) for one bus. Lit numerals are the
 * channels routed to this bus; clicking toggles between the bus and THRU.
 */
const meta = {
	title: 'Contracts/Mixer/AssignMatrix',
	component: AssignMatrix,
	tags: ['autodocs'],
	args: {
		bus: 'A',
		assigns: { 1: 'A', 2: 'THRU', 3: 'B', 4: 'THRU' },
		onassign: fn()
	},
	argTypes: {
		bus: { control: 'inline-radio', options: ['A', 'B'] }
	}
} satisfies Meta<typeof AssignMatrix>;
export default meta;
type Story = StoryObj<typeof meta>;

export const BusA: Story = contracts.BusA;
export const BusB: Story = contracts.BusB;

/** Every channel THRU: nothing lit, tooltips offer to assign. */
export const AllThru: Story = {
	args: { assigns: { 1: 'THRU', 2: 'THRU', 3: 'THRU', 4: 'THRU' } },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelectorAll('.cell')).toHaveLength(4);
		await expect(canvasElement.querySelectorAll('.cell.lit')).toHaveLength(0);
		await expect(within(canvasElement).getAllByTitle(/is THRU \(not on the crossfader\) now/)).toHaveLength(4);
	}
};

/** All four channels on this bus: every cell lit. */
export const AllAssigned: Story = {
	args: { assigns: { 1: 'A', 2: 'A', 3: 'A', 4: 'A' } },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelectorAll('.cell')).toHaveLength(4);
		await expect(within(canvasElement).getAllByTitle(/is on left \(A\) now/)).toHaveLength(4);
	}
};
