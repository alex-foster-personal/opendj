import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, within } from 'storybook/test';

import { contracts } from './BannerWarning.contract';
import BannerWarning from './BannerWarning.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = {
	title: 'Contracts/Root/BannerWarning',
	component: BannerWarning,
	tags: ['autodocs'],
	argTypes: { message: { control: 'text' } }
} satisfies Meta<typeof BannerWarning>;
export default meta;
type Story = StoryObj<typeof meta>;

export const ReadOnly: Story = contracts.ReadOnly;
export const DaemonDown: Story = contracts.DaemonDown;

/** A short single-line warning. */
export const Short: Story = {
	args: { message: 'Library is read-only while rekordbox is open.' },
	play: async ({ canvasElement, args }) => {
		await expect(within(canvasElement).getByRole('alert').textContent).toBe(args.message);
	}
};

/** A long message, to check wrapping inside the banner. */
export const LongMessage: Story = {
	args: {
		message:
			'Some tracks under /music/Crates/Warehouse could not be found. They may be on a drive that is not mounted, or they may have been moved. Reconnect the drive and re-check, or relink the missing files from the library screen.'
	},
	play: async ({ canvasElement, args }) => {
		await expect(within(canvasElement).getByRole('alert').textContent).toBe(args.message);
	}
};

/** Empty string: the alert region renders with no text. */
export const Empty: Story = {
	args: { message: '' },
	play: async ({ canvasElement, args }) => {
		await expect(within(canvasElement).getByRole('alert').textContent).toBe(args.message);
	}
};
