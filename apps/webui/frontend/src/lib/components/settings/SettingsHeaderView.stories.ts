import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import SettingsHeaderView from './SettingsHeaderView.svelte';

/**
 * The settings overlay header: history back and forward, the search box and
 * Esc. The container owns the navigation history and the query.
 */
const meta = {
	title: 'Components/Settings/SettingsHeaderView',
	component: SettingsHeaderView,
	tags: ['autodocs'],
	args: {
		canBack: false,
		canForward: false,
		onBack: fn(),
		onForward: fn(),
		query: '',
		onQueryInput: fn(),
		onSearchKeydown: fn(),
		onClose: fn(),
		onHeadPointerDown: fn(),
		onHeadPointerMove: fn(),
		onHeadPointerUp: fn()
	}
} satisfies Meta<typeof SettingsHeaderView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Freshly opened: no history, empty search. */
export const Fresh: Story = {};

/** After navigating: back and forward are both available, with a query typed. */
export const WithHistory: Story = { args: { canBack: true, canForward: true, query: 'theme' } };
