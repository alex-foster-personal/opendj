import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect } from 'storybook/test';

import { contracts } from './FaderRejoinGuidance.contract';
import FaderRejoinGuidance from './FaderRejoinGuidance.svelte';

/**
 * Yellow arrow showing where a physical fader must travel to rejoin the
 * on-screen value. It is absolutely positioned (inset 0) over its nearest
 * positioned ancestor and pulses on a 3s fade loop.
 */
const meta = {
	title: 'Contracts/Mixer/FaderRejoinGuidance',
	component: FaderRejoinGuidance,
	tags: ['autodocs'],
	parameters: { layout: 'fullscreen' },
	args: { fromY: 200, toY: 60, trackWidth: 120 }
} satisfies Meta<typeof FaderRejoinGuidance>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Short: Story = contracts.Short;
export const Long: Story = contracts.Long;

/** Long travel (over 24px): the arrow shaft is dotted. */
export const LongTravelUp: Story = {
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('svg.rejoin-guidance line')).toHaveAttribute('y1', '200');
		await expect(canvasElement.querySelector('svg.rejoin-guidance line')).toHaveAttribute('y2', '60');
	}
};

/** Fader needs to move down: arrowhead points toward larger Y. */
export const LongTravelDown: Story = {
	args: { fromY: 60, toY: 200 },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('svg.rejoin-guidance line')).toHaveAttribute('y1', '60');
		await expect(canvasElement.querySelector('svg.rejoin-guidance line')).toHaveAttribute('y2', '200');
	}
};

/** Short travel (24px or less): the shaft is solid. */
export const ShortTravel: Story = {
	args: { fromY: 100, toY: 84 },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('svg.rejoin-guidance line')).toHaveAttribute('y1', '100');
		await expect(canvasElement.querySelector('svg.rejoin-guidance line')).toHaveAttribute('y2', '84');
	}
};

/** Zero length: from equals to, so only the arrowhead marker remains. */
export const NoTravel: Story = {
	args: { fromY: 100, toY: 100 },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('svg.rejoin-guidance line')).toHaveAttribute('y1', '100');
		await expect(canvasElement.querySelector('svg.rejoin-guidance line')).toHaveAttribute('y2', '100');
	}
};
