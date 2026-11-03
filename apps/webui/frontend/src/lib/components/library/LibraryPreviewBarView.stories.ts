import type { Meta, StoryObj } from '@storybook/sveltekit';
import { createRawSnippet } from 'svelte';
import { fn } from 'storybook/test';

import LibraryPreviewBarView from './LibraryPreviewBarView.svelte';

/** A flat stand-in for the overview strip, which is a live container in the app. */
const strip = createRawSnippet(() => ({
	render: () =>
		'<div style="flex:1;height:20px;background:linear-gradient(90deg,var(--rb-border),var(--rb-panel-raised))"></div>'
}));

/**
 * The Crate Desk preview bar (LIBWS-04): the focused track's identity, its
 * overview strip and four readouts. Tracks are invented, and artwork_available
 * is false so no artwork request is made.
 */
const meta = {
	title: 'Components/Library/LibraryPreviewBarView',
	component: LibraryPreviewBarView,
	tags: ['autodocs'],
	parameters: { layout: 'fullscreen' },
	args: {
		previewStrip: strip,
		row: {
			stable_id: 'a'.repeat(40),
			title: 'Night Drive (Extended Mix)',
			artist: 'Lumen Fields',
			bpm: 124,
			key: '8A',
			duration_ms: 412_000,
			energy: 6,
			artwork_available: false
		},
		playing: false,
		elsewhere: false,
		stopHere: false,
		playTitle: 'Play a preview of this track (Library mode plays it without starting the decks)',
		refusal: null,
		previewStatus: 'idle' as const,
		otherPreviewStatus: 'idle' as const,
		keyLabel: '8A',
		onPlay: fn(),
		onStop: fn()
	}
} satisfies Meta<typeof LibraryPreviewBarView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A focused track with every readout. */
export const Focused: Story = {};

/** No analysis yet: readouts are blank, and the untitled fallback shows. */
export const Unanalyzed: Story = {
	args: {
		row: {
			stable_id: 'b'.repeat(40),
			title: null,
			artist: null,
			bpm: null,
			key: null,
			duration_ms: null,
			energy: null,
			artwork_available: null
		},
		keyLabel: ''
	}
};

/** A long title and artist ellipsize inside the identity column. */
export const LongTitle: Story = {
	args: {
		row: {
			stable_id: 'c'.repeat(40),
			title: 'Late Night Peak Hour Rolling Basslines and Other Slow Burners (Archive 2019 Remaster)',
			artist: 'The Quiet Kiln Collective featuring Every Guest Vocalist',
			bpm: 128.04,
			key: '11B',
			duration_ms: 3_725_000,
			energy: 9,
			artwork_available: false
		},
		keyLabel: '11B'
	}
};

/** Nothing selected. */
export const Empty: Story = {
	args: {
		row: null,
		playTitle: 'Select a track to preview it',
		keyLabel: ''
	}
};

/** Preview is playing on the focused row. */
export const Playing: Story = {
	args: {
		playing: true,
		previewStatus: 'playing',
		playTitle: 'Pause the preview'
	}
};
