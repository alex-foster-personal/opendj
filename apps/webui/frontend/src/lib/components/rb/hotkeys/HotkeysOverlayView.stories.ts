import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import { HOTKEY_REGISTRY, HOTKEYS_OVERLAY_TRIGGER } from './hotkeys-registry';

import HotkeysOverlayView from './HotkeysOverlayView.svelte';

/**
 * The LIBUX-04 hotkeys overlay: grid of grouped cards (default) or a flat
 * list, with live type-to-filter and yellow match marks in both. The entries
 * are the app's real HOTKEY_REGISTRY, a frozen table, so each story shows the
 * bindings the app actually ships; the filter is the real filterHotkeys.
 */
const meta = {
	title: 'Components/Hotkeys/HotkeysOverlayView',
	component: HotkeysOverlayView,
	tags: ['autodocs'],
	parameters: { layout: 'fullscreen' },
	args: {
		open: true,
		view: 'grid',
		query: '',
		entries: HOTKEY_REGISTRY,
		trigger: HOTKEYS_OVERLAY_TRIGGER,
		onHide: fn(),
		onSetView: fn(),
		onSetQuery: fn()
	}
} satisfies Meta<typeof HotkeysOverlayView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Grid view, no filter: every binding, grouped into cards. */
export const Grid: Story = {};

/** List view: one row per binding, its group on the right. */
export const List: Story = { args: { view: 'list' } };

/** Filtered: matching text marked in yellow in chord, description and group. */
export const Filtered: Story = { args: { query: 'overlay' } };

/** Filtered list view. */
export const FilteredList: Story = { args: { view: 'list', query: 'deck' } };

/** A filter nothing matches. */
export const NoMatch: Story = { args: { query: 'zzqx' } };

/** An empty registry: the overlay says no bindings are registered. */
export const NoBindings: Story = { args: { entries: [] } };

/** Closed: nothing is drawn. */
export const Closed: Story = { args: { open: false }, tags: ['renders-nothing'] };
