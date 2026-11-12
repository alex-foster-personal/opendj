import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn, userEvent } from 'storybook/test';

import SaveOrderButton from './SaveOrderButton.svelte';

/** "Save this order" (LIBUX-8678): idle saves on press, busy is disabled. */
const meta = {
	title: 'Library/Browser/SaveOrderButton',
	component: SaveOrderButton,
	tags: ['autodocs'],
	args: { busy: false, onsave: fn() }
} satisfies Meta<typeof SaveOrderButton>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Pressing the idle button calls onsave once. */
export const Idle: Story = {
	play: async ({ canvasElement, args }) => {
		const button = canvasElement.querySelector<HTMLButtonElement>(
			'[data-testid="save-playlist-order"]'
		);
		await expect(button).not.toBeNull();
		await userEvent.click(button!);
		await expect(args.onsave).toHaveBeenCalledTimes(1);
	}
};

/** While saving, the button is disabled and says so. */
export const Busy: Story = {
	args: { busy: true },
	play: async ({ canvasElement }) => {
		const button = canvasElement.querySelector<HTMLButtonElement>(
			'[data-testid="save-playlist-order"]'
		);
		await expect(button?.disabled).toBe(true);
		await expect(button?.textContent?.trim()).toBe('Saving order...');
	}
};
