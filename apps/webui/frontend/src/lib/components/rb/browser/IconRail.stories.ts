import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import IconRail from './IconRail.svelte';

/**
 * Reference story for the inert-control rule - see the "Controls without a
 * data source render inert" decision doc.
 *
 * The rail is the clearest example in the codebase: Spotify is the only
 * source wired to real data, so it alone is clickable. Every other glyph is
 * `disabled`, carries `.rb-inert`, and its title comes from
 * `plannedTitle(id)` (name + what it will do + Not built yet). Hover any
 * dimmed icon to see it.
 */
const meta = {
	title: 'Components/Browser/IconRail',
	component: IconRail,
	tags: ['autodocs'],
	argTypes: {
		source: { control: 'inline-radio', options: ['collection', 'spotify'] }
	},
	// Required prop. `fn` reports the click in the Actions panel rather than
	// faking a Spotify response - the rail only raises the event, the parent
	// owns the real fetch.
	args: { onspotify: fn() }
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
