import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import HistoryPanelView from './HistoryPanelView.svelte';

/**
 * Play History in the tree slot (PLAYS-9745). Stateless: it renders the nights
 * list, or one night's plays in play order, from props and never fetches.
 * HistorySourcePanel.svelte is the wrapper that loads the data. Each story is
 * one state the wrapper can hand it.
 */
const meta = {
	title: 'Components/Browser/HistoryPanelView',
	component: HistoryPanelView,
	tags: ['autodocs'],
	// `fn` reports each click in the Actions panel; the wrapper owns the fetches.
	args: {
		days: [
			{ date: '2026-11-12', count: 3 },
			{ date: '2026-11-07', count: 41 }
		],
		loading: false,
		error: null,
		night: null,
		plays: [],
		playsLoading: false,
		playsError: null,
		saving: false,
		oncollection: fn(),
		onopen: fn(),
		onback: fn(),
		onsave: fn(),
		ondaymenu: fn(),
		onplaymenu: fn()
	}
} satisfies Meta<typeof HistoryPanelView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The nights list, newest first. */
export const Nights: Story = {};

/** Nothing logged yet: the panel says how plays get here. */
export const NoPlaysYet: Story = { args: { days: [] } };

/** The nights request failed: the reason is shown, never an empty list. */
export const NightsFailed: Story = { args: { days: [], error: 'daemon returned 500' } };

/** One night open, in play order; the last track has left the collection. */
export const OneNight: Story = {
	args: {
		night: '2026-11-12',
		plays: [
			{
				position: 1,
				stable_id: 'a1',
				play_id: 'p1',
				time: '22:04',
				title: 'Opening Track',
				artist: 'First Artist',
				gone: false
			},
			{
				position: 2,
				stable_id: 'b2',
				play_id: 'p2',
				time: '22:09',
				title: 'Second Track',
				artist: 'Second Artist',
				gone: false
			},
			{
				position: 3,
				stable_id: 'c3',
				play_id: null,
				time: '22:15',
				title: 'Removed Track',
				artist: 'Third Artist',
				gone: true
			}
		]
	}
};
