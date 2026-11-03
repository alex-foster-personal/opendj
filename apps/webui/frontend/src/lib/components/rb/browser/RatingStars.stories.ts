import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn } from 'storybook/test';

import { contracts } from './RatingStars.contract';
import RatingStars from './RatingStars.svelte';

const meta = {
	title: 'Contracts/Browser/RatingStars',
	component: RatingStars,
	tags: ['autodocs'],
	args: { rating: 3, onrate: fn() }
} satisfies Meta<typeof RatingStars>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Rated: Story = contracts.Rated;
export const Unrated: Story = contracts.Unrated;
export const Disabled: Story = contracts.Disabled;

export const ThreeStars: Story = {
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelectorAll('.rb-star.filled')).toHaveLength(3);
		await expect(canvasElement.querySelector('[aria-label="Set rating 3"]')).toHaveAttribute('aria-checked', 'true');
		await expect(canvasElement.querySelector('[aria-label="Set rating 4"]')).toHaveAttribute('aria-checked', 'false');
	}
};

/** rating 0 is the cleared state and looks the same as unrated. */
export const Cleared: Story = {
	args: { rating: 0 },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.rb-stars')).toHaveClass('unrated');
		await expect(canvasElement.querySelectorAll('.rb-star.filled')).toHaveLength(0);
	}
};

export const FiveStars: Story = {
	args: { rating: 5 },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelectorAll('.rb-star.filled')).toHaveLength(5);
		await expect(canvasElement.querySelector('.rb-stars')).not.toHaveClass('unrated');
	}
};
