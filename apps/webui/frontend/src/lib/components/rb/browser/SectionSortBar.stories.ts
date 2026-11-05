import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import SectionSortBar from './SectionSortBar.svelte';

/** The strip shown while a playlist with sections is sorted (R-SEC-2). */
const meta = {
	title: 'Components/Browser/SectionSortBar',
	component: SectionSortBar,
	tags: ['autodocs'],
	args: { onwithin: fn(), onrestore: fn() }
} satisfies Meta<typeof SectionSortBar>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Sorted across the whole playlist: sections are hidden. */
export const SortedAcross: Story = { args: { within: false } };

/** Sorted inside each section: section headers stay. */
export const SortedWithin: Story = { args: { within: true } };
