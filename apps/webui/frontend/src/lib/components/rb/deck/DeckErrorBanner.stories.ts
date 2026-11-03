import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn, within } from 'storybook/test';

import { contracts } from './DeckErrorBanner.contract';
import DeckErrorBanner from './DeckErrorBanner.svelte';

const meta = {
	title: 'Contracts/Deck/DeckErrorBanner',
	component: DeckErrorBanner,
	tags: ['autodocs'],
	parameters: { layout: 'padded' },
	args: {
		deckId: 1,
		error: "couldn't load /music/Lumen Fields - Night Drive (Extended Mix).flac: decode failed",
		errorId: 'E-7F3A21',
		onDismiss: fn()
	}
} satisfies Meta<typeof DeckErrorBanner>;
export default meta;
type Story = StoryObj<typeof meta>;

export const WithId: Story = contracts.WithId;
export const NoIdYet: Story = contracts.NoIdYet;
export const LoadFailure: Story = contracts.LoadFailure;

/** A non-load failure on another deck. */
export const ProcessorError: Story = {
	args: { deckId: 3, error: 'audio worklet processor stopped: out of memory', errorId: 'E-19C0D4' },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByRole('alert')).toHaveTextContent('audio worklet processor stopped: out of memory');
		await expect(canvasElement.querySelector('.deck-error-id')).toHaveTextContent('E-19C0D4');
	}
};

/** Very long message: the headline must truncate, not grow the deck. */
export const LongMessage: Story = {
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.deck-error-text')).toHaveTextContent("couldn't load /music/Archive/Unsorted/2019/");
	},
	args: {
		error:
			"couldn't load /music/Archive/Unsorted/2019/Lumen Fields - Night Drive (Extended Mix) (Remastered Version With Extra Long Filename For Layout Testing).flac: the file ended unexpectedly while reading the audio frame table"
	}
};
