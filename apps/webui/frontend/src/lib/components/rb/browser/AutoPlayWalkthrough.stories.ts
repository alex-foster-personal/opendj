import type { Meta, StoryObj } from '@storybook/sveltekit';

import AutoPlayWalkthrough from './AutoPlayWalkthrough.svelte';

/**
 * Looping animated explainer driven by the pure autoplay-walkthrough
 * timeline. With prefers-reduced-motion set it renders the final frame.
 */
const meta = {
	title: 'Components/Browser/AutoPlayWalkthrough',
	component: AutoPlayWalkthrough,
	tags: ['autodocs'],
	argTypes: { mode: { control: 'inline-radio', options: ['greedy', 'reach', 'enforce'] } }
} satisfies Meta<typeof AutoPlayWalkthrough>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Greedy: picks the best next match each step and can strand tracks. */
export const Greedy: Story = { args: { mode: 'greedy' } };

/** Reach: looks ahead so more tracks stay reachable. */
export const Reach: Story = { args: { mode: 'reach' } };

/** Enforce: strict compatibility rules, skipped rows compact into the tray. */
export const Enforce: Story = { args: { mode: 'enforce' } };
