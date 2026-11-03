import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect } from 'storybook/test';

import ModeThumbnail from './ModeThumbnail.svelte';

/** App-mode chooser tile picture, one paint per mode. Decorative: aria-hidden. */
const meta = {
	title: 'Components/Rb/ModeThumbnail',
	component: ModeThumbnail,
	tags: ['autodocs', 'capture-canvas-width'],
	args: { iconId: 'library' }
} satisfies Meta<typeof ModeThumbnail>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Library: Story = {
	args: { iconId: 'library' },
	play: async ({ canvasElement }) => {
		const tile = canvasElement.querySelector<HTMLElement>('.mode-thumbnail');
		await expect(tile).toHaveClass('mode-thumbnail', 'library');
	}
};
export const Prep: Story = {
	args: { iconId: 'prep' },
	play: async ({ canvasElement }) => {
		const tile = canvasElement.querySelector<HTMLElement>('.mode-thumbnail');
		await expect(tile).toHaveClass('prep');
	}
};
export const Gig: Story = {
	args: { iconId: 'gig' },
	play: async ({ canvasElement }) => {
		const tile = canvasElement.querySelector<HTMLElement>('.mode-thumbnail');
		await expect(tile).toHaveClass('gig');
	}
};
export const Trackify: Story = {
	args: { iconId: 'trackify' },
	play: async ({ canvasElement }) => {
		const tile = canvasElement.querySelector<HTMLElement>('.mode-thumbnail');
		await expect(tile).toHaveClass('trackify');
	}
};
