import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect } from 'storybook/test';

import { contracts } from './PadModeMenu.contract';
import PadModeMenu from './PadModeMenu.svelte';

/**
 * Closed by default; click HOT CUE to open the menu. Only Hot Cue is built,
 * every other entry is labeled not-built-yet and renders inert with a
 * tooltip, per the inert-control rule.
 */
const meta = {
	title: 'Contracts/Deck/PadModeMenu',
	component: PadModeMenu,
	tags: ['autodocs'],
	parameters: { layout: 'padded' },
	argTypes: { deckId: { control: 'inline-radio', options: [1, 2, 3, 4] }, stacked: { control: 'boolean' } },
	args: { deckId: 1, stacked: false }
} satisfies Meta<typeof PadModeMenu>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Closed: Story = contracts.Closed;
export const Open: Story = contracts.Open;
export const Dismiss: Story = contracts.Dismiss;
export const Stacked: Story = contracts.Stacked;

/** Single-line trigger (default closed). */
export const Default: Story = {
	play: async ({ canvasElement }) => {
		const trigger = canvasElement.querySelector('[data-testid="hot-cue-menu-deck-1"]');
		await expect(trigger).toHaveAttribute('aria-expanded', 'false');
		await expect(canvasElement.querySelector('.pad-menu')).toBeNull();
	}
};

/** A different deck slot; only the test ids change. */
export const Deck3: Story = {
	args: { deckId: 3 },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('[data-testid="hot-cue-menu-deck-3"]')).toHaveAttribute('aria-label', 'hot cue menu deck 3');
	}
};
