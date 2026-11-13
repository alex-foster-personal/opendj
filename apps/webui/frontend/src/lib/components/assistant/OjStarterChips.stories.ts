import type { Meta, StoryObj } from '@storybook/sveltekit';

import { OJ_STARTERS } from '$lib/assistant/oj-starters';
import OjStarterChips from './OjStarterChips.svelte';
import { OJ_MOTION_FULL } from './oj-glass-motion';

/** OJ's starter chips for an empty agent sidebar (#14040): a click fills the composer, never sends. */
const meta = {
	title: 'Components/OjStarterChips',
	component: OjStarterChips,
	tags: ['autodocs'],
	args: { starters: OJ_STARTERS.library, motion: OJ_MOTION_FULL, onpick: () => {} }
} satisfies Meta<typeof OjStarterChips>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Library: Story = {};
export const Performance: Story = { args: { starters: OJ_STARTERS.performance } };
/** Reduced motion, or a deck playing on /performance: chips appear at once, no stagger. */
export const Still: Story = {
	args: { motion: { idleGestures: false, bubbles: false, fillMode: 'static' } }
};
