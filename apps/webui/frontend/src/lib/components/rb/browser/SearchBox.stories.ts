import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import SearchBox from './SearchBox.svelte';

const meta = {
	title: 'Components/Browser/SearchBox',
	component: SearchBox,
	tags: ['autodocs'],
	argTypes: { mode: { control: 'inline-radio', options: ['filter', 'find', 'collection'] } },
	args: { value: '', oninput: fn(), onclear: fn(), onescapeclear: fn(), onfocuschange: fn() }
} satisfies Meta<typeof SearchBox>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Empty list filter (default mode). */
export const Empty: Story = {};

export const FilterWithText: Story = { args: { value: 'night drive' } };

/** In-place find: highlights matches without filtering. */
export const Find: Story = { args: { mode: 'find', value: 'lumen' } };

/** Whole-collection full-text search. */
export const Collection: Story = { args: { mode: 'collection', value: 'bpm:>=124 key:8A' } };

export const CustomPlaceholder: Story = { args: { placeholder: 'Search this crate' } };

export const LongQuery: Story = {
	args: {
		value: 'artist:"Lumen Fields" genre:house bpm:>=120 bpm:<=128 key:8A year:2024 label:Harbor'
	}
};
