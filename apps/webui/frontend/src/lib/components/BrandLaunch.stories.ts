import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn } from 'storybook/test';

import BrandLaunch from './BrandLaunch.svelte';
import { contracts } from './BrandLaunch.contract';

/**
 * Full-screen launch animation. By default it plays only when the per-browser
 * completion marker (localStorage key odj.brand-launch.v1) is not set, and
 * sets that marker when the animation ends, so it plays once per browser
 * profile. The stories pass shouldPlay and onfinish themselves, so they never
 * read or write that marker. Honors prefers-reduced-motion (static mark, still fades).
 */
const meta = {
	title: 'Contracts/Root/BrandLaunch',
	component: BrandLaunch,
	tags: ['autodocs'],
	parameters: { layout: 'fullscreen' }
} satisfies Meta<typeof BrandLaunch>;
export default meta;
type Story = StoryObj<typeof meta>;

export const FirstLaunch: Story = contracts.FirstLaunch;
export const AlreadyPlayed: Story = contracts.AlreadyPlayed;

/** First launch with mocked finish handler (autodocs / Actions panel). */
export const FirstLaunchInteractive: Story = {
	args: { shouldPlay: () => true, onfinish: fn(), pinned: true },
	play: async ({ canvasElement }) => {
		const overlay = canvasElement.ownerDocument.querySelector<HTMLElement>('[aria-label="Open DJ launch animation"]');
		await expect(overlay).not.toBeNull();
		await expect(overlay).toHaveClass('brand-launch--pinned');
		const dark = overlay?.querySelector('[data-testid="brand-launch-half-dark"]');
		await expect(dark?.getAnimations().length).toBe(0);
	}
};
