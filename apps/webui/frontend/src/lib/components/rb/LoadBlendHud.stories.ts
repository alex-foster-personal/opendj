import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect } from 'storybook/test';

import { contracts } from './LoadBlendHud.contract';
import LoadBlendHud from './LoadBlendHud.svelte';

/** Readout for the Load-to-CH click-drag intro blend. Display only. */
const meta = {
	title: 'Contracts/Root/LoadBlendHud',
	component: LoadBlendHud,
	tags: ['autodocs']
} satisfies Meta<typeof LoadBlendHud>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Forward: Story = contracts.Forward;
export const Backward: Story = contracts.Backward;
export const Zero: Story = contracts.Zero;

/** Blend just started: nothing scrubbed. */
export const AtStart: Story = {
	args: { t: 0, fader: 0, scrubDeltaMs: 0 },
	play: async ({ canvasElement }) => {
		const hud = canvasElement.querySelector('[data-testid="load-blend-hud"]');
		await expect(hud).toHaveTextContent('t 0.00');
		await expect(hud).toHaveTextContent('fader 0.00');
	}
};

/** Dragged forward: scrub shows a leading plus sign. */
export const ScrubForward: Story = {
	args: { t: 0.42, fader: 0.5, scrubDeltaMs: 180 },
	play: async ({ canvasElement }) => {
		const hud = canvasElement.querySelector('[data-testid="load-blend-hud"]');
		await expect(hud).toHaveTextContent('t 0.42');
		await expect(hud).toHaveTextContent('fader 0.50');
		await expect(hud).toHaveTextContent('180 ms');
	}
};

/** Dragged backward: the minus comes from the number itself. */
export const ScrubBackward: Story = {
	args: { t: 0.75, fader: 0.9, scrubDeltaMs: -240 },
	play: async ({ canvasElement }) => {
		const hud = canvasElement.querySelector('[data-testid="load-blend-hud"]');
		await expect(hud).toHaveTextContent('t 0.75');
		await expect(hud).toHaveTextContent('fader 0.90');
		await expect(hud).toHaveTextContent('240 ms');
	}
};

export const Complete: Story = {
	args: { t: 1, fader: 1, scrubDeltaMs: 0 },
	play: async ({ canvasElement }) => {
		const hud = canvasElement.querySelector('[data-testid="load-blend-hud"]');
		await expect(hud).toHaveTextContent('t 1.00');
		await expect(hud).toHaveTextContent('fader 1.00');
	}
};
