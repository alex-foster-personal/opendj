import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn, within } from 'storybook/test';

import BrowserConfirmDialog from './BrowserConfirmDialog.svelte';
import { contracts } from './BrowserConfirmDialog.contract';

const meta = {
	title: 'Contracts/Browser/BrowserConfirmDialog',
	component: BrowserConfirmDialog,
	tags: ['autodocs'],
	args: {
		open: true,
		title: 'Remove from playlist?',
		message: 'Remove 3 tracks from "Warm-up Set"? The tracks stay in your library.',
		onPrimary: fn(),
		onSecondary: fn()
	}
} satisfies Meta<typeof BrowserConfirmDialog>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Open: Story = contracts.Open;
export const Defaults: Story = contracts.Defaults;
export const DefaultOnlySecondary: Story = contracts.DefaultOnlySecondary;
export const BothBoxes: Story = contracts.BothBoxes;
export const Closed: Story = contracts.Closed;

/** Defaults: OK / Cancel with the "don't show again" checkbox. */
export const Default: Story = {
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByRole('dialog', { name: 'Remove from playlist?' })).toBeInTheDocument();
		await expect(within(canvasElement).getByText(/Remove 3 tracks/)).toBeInTheDocument();
	}
};

/** Both optional checkboxes shown, with custom button labels. */
export const WithDefaultChoice: Story = {
	args: {
		primaryLabel: 'Remove',
		secondaryLabel: 'Keep',
		showDefault: true,
		defaultLabel: 'Always remove from this playlist'
	},
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getByRole('button', { name: 'Remove' })).toBeInTheDocument();
		await expect(canvas.getByRole('button', { name: 'Keep' })).toBeInTheDocument();
		await expect(canvas.getByText('Always remove from this playlist')).toBeInTheDocument();
	}
};

/** No checkboxes at all. */
export const NoCheckboxes: Story = {
	args: { showRemember: false },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByRole('dialog', { name: 'Remove from playlist?' })).toBeInTheDocument();
		await expect(canvasElement.querySelectorAll('.browser-confirm-opt').length).toBeLessThan(2);
	}
};

/** Long title and message must wrap inside the dialog. */
export const LongText: Story = {
	args: {
		title: 'Replace the cue points on every selected track with the analyzed grid positions?',
		message:
			'This rewrites hot cues and memory cues on 128 tracks, including "Night Drive (Extended Mix)" by Lumen Fields, using the beatgrid detected during analysis. Existing manual cue labels are kept where the position is unchanged.'
	},
	play: async ({ canvasElement, args }) => {
		await expect(within(canvasElement).getByRole('dialog', { name: args.title })).toBeInTheDocument();
	}
};
