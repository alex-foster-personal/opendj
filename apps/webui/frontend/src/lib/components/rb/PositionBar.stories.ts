import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, within } from 'storybook/test';

import { contracts } from './PositionBar.contract';
import PositionBar from './PositionBar.svelte';

/** Thin track-position bar. Hover the fill for the raw ms readout. */
const meta = {
	title: 'Contracts/Root/PositionBar',
	component: PositionBar,
	tags: ['autodocs', 'capture-canvas-width']
} satisfies Meta<typeof PositionBar>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Halfway: Story = contracts.Halfway;
export const UnknownDuration: Story = contracts.UnknownDuration;
export const PastEnd: Story = contracts.PastEnd;

export const Start: Story = {
	args: { positionMs: 0, durationMs: 240000 },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByTitle('0 ms of 240000 ms')).toBeInTheDocument();
	}
};

export const Midway: Story = { args: { positionMs: 120000, durationMs: 240000 },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByTitle('120000 ms of 240000 ms')).toBeInTheDocument();
	}
};

export const Finished: Story = { args: { positionMs: 240000, durationMs: 240000 },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByTitle('240000 ms of 240000 ms')).toBeInTheDocument();
	}
};

/** Position beyond duration clamps to 100%. */
export const OverRun: Story = {
	args: { positionMs: 300000, durationMs: 240000 },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByTitle('300000 ms of 240000 ms')).toBeInTheDocument();
	}
};

/** Unknown duration with an explicit hover label. */
export const UnknownDurationLabeled: Story = {
	args: { positionMs: 30000, durationMs: null, label: 'Night Drive (Extended Mix) position' },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByTitle('30000 ms of unknown ms')).toBeInTheDocument();
	}
};
