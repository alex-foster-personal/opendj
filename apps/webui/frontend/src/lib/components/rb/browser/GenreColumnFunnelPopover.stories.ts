import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import GenreColumnFunnelPopover from './GenreColumnFunnelPopover.svelte';
import { EMPTY_GENRE_FILTER } from './track-table-column-filters';

const ROWS = [
	{ genre: 'Tech House' },
	{ genre: 'Tech House' },
	{ genre: 'techno' },
	{ genre: 'Techno' },
	{ genre: 'Deep House' },
	{ genre: null }
];

/** The Genre column funnel: the genres in the current list with row counts, multi-select. */
const meta = {
	title: 'Components/Browser/GenreColumnFunnelPopover',
	component: GenreColumnFunnelPopover,
	tags: ['autodocs'],
	args: { filter: EMPTY_GENRE_FILTER, rows: ROWS, onpatch: fn() }
} satisfies Meta<typeof GenreColumnFunnelPopover>;
export default meta;
type Story = StoryObj<typeof meta>;

export const NoFilter: Story = {};
export const TwoGenresPicked: Story = { args: { filter: { selected: ['Tech House', 'Techno'] } } };
export const EmptyList: Story = { args: { rows: [] } };
