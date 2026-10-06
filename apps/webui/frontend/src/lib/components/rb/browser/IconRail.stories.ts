import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import IconRail from './IconRail.svelte';

/**
 * Reference story for the inert-control rule - see the "Controls without a
 * data source render inert" decision doc.
 *
 * The rail is the clearest example in the codebase: the top-left home entry
 * (back to All Tracks) and Spotify are wired to real data, so they are
 * clickable. The two planned glyphs (USB export, play history) are
 * `disabled`, carry `.rb-inert`, and take their title from
 * `plannedTitle(id)` (name + what it will do + Not built yet). Hover a
 * dimmed icon to see it. Glyphs with no planned feature were removed
 * (UX-RAIL-01).
 */
const meta = {
	title: 'Components/Browser/IconRail',
	component: IconRail,
	tags: ['autodocs'],
	argTypes: {
		source: { control: 'inline-radio', options: ['collection', 'spotify'] }
	},
	// Required props. `fn` reports the click in the Actions panel rather than
	// faking a Spotify response - the rail only raises the event, the parent
	// owns the real fetch.
	args: { onspotify: fn(), oncollection: fn() }
} satisfies Meta<typeof IconRail>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Default: collection selected, so no rail button reads as active. */
export const Collection: Story = {
	args: { source: 'collection' }
};

/** The one live source, showing the active inset-accent treatment. */
export const SpotifyActive: Story = {
	args: { source: 'spotify' }
};
