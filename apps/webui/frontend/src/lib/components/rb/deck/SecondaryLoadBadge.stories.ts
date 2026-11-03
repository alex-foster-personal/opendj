import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, within } from 'storybook/test';

import { contracts } from './SecondaryLoadBadge.contract';
import SecondaryLoadBadge from './SecondaryLoadBadge.svelte';

const meta = {
	title: 'Contracts/Deck/SecondaryLoadBadge',
	component: SecondaryLoadBadge,
	tags: ['autodocs'],
	parameters: { layout: 'padded' },
	argTypes: { status: { control: 'select', options: ['unavailable', 'loading', 'ready', 'error'] } },
	args: { status: 'loading', error: null, phase: null }
} satisfies Meta<typeof SecondaryLoadBadge>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Loading: Story = contracts.Loading;
export const LoadingNoPhase: Story = contracts.LoadingNoPhase;
export const Failed: Story = contracts.Failed;
export const Settled: Story = contracts.Settled;

/** Loading with a named phase: the hover tip carries it. */
export const LoadingDecoding: Story = {
	args: { phase: 'decoding' },
	play: async ({ canvasElement }) => {
		const badge = within(canvasElement).getByTestId('deck-secondary-load');
		await expect(badge).toHaveAttribute('data-stem-phase', 'decoding');
		await expect(badge.getAttribute('title') ?? '').toContain('(decoding)');
	}
};

/** Settled with stems ready: renders nothing. */
export const ReadyRendersNothing: Story = {
	tags: ['renders-nothing'],
	args: { status: 'ready' },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('[data-testid="deck-secondary-load"]')).toBeNull();
	}
};

/** Settled with no bundle: renders nothing. */
export const UnavailableRendersNothing: Story = {
	tags: ['renders-nothing'],
	args: { status: 'unavailable' },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('[data-testid="deck-secondary-load"]')).toBeNull();
	}
};
