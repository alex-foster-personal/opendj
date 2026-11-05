import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import TempoPrefPopoverView from './TempoPrefPopoverView.svelte';

/**
 * The preferred-tempo editor (PREF-01) opened by right-clicking a BPM. The
 * container fetches the track and saves with its etag; here the fields are
 * args. Escape or a click outside raises `onclose`. The panel is fixed, so
 * each story places it near the top left.
 */
const meta = {
	title: 'Components/TempoPrefPopoverView',
	component: TempoPrefPopoverView,
	tags: ['autodocs'],
	parameters: { layout: 'fullscreen' },
	args: {
		position: { x: 16, y: 16 },
		loading: false,
		loadError: null,
		regular: 124,
		min: 120,
		max: 128,
		rangeInvalid: false,
		saving: false,
		saveError: null,
		onSave: fn(),
		onClear: fn(),
		onclose: fn()
	}
} satisfies Meta<typeof TempoPrefPopoverView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A preference and range already set. */
export const Preferred: Story = {};

/** No preference stored: every field reads unset. */
export const Unset: Story = { args: { regular: null, min: null, max: null } };

/** Min at or above max: the message shows and Save is disabled. */
export const RangeInvalid: Story = { args: { min: 130, max: 126, rangeInvalid: true } };

/** Save in flight: every button disabled. */
export const Saving: Story = { args: { saving: true } };

/** A save failed. */
export const SaveError: Story = {
	args: { saveError: 'Save failed: Error: 500 Internal Server Error' }
};

/** The fresh track read is in flight. */
export const Loading: Story = { args: { loading: true } };

/** The track could not be read. */
export const LoadError: Story = {
	args: { loadError: 'Failed to load track: Error: 404 Not Found' }
};
