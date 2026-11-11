import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import type { Track } from '$lib/api';

import ConflictDialog from './ConflictDialog.svelte';

/**
 * Pure props: `mine` is the operator's unsaved edit, `current` the server's
 * copy. The three buttons only raise callbacks (recorded in the Actions
 * panel); resolving a conflict is the parent's job. The track below is
 * invented and nothing is fetched.
 */
const serverTrack = (over: Partial<Track> = {}): Track => ({
	stable_id: 'c'.repeat(40),
	title: 'Night Drive (Extended Mix)',
	artist: 'Lumen Fields',
	album: 'Quiet Kiln',
	bpm: 124,
	key: '8A',
	rating: 3,
	notes: 'warm intro',
	tags: ['peak', 'vocal'],
	play_count: 4,
	provenance: {},
	artwork_available: false,
	auto_cues_available: false,
	has_rb_mapping: true,
	lyrics_available: false,
	stems_available: false,
	tracklist_hidden: false,
	created_at: '2026-09-01T10:00:00.000000Z',
	updated_at: '2026-10-12T21:30:00.000000Z',
	...over
});

const meta = {
	title: 'Components/ConflictDialog',
	component: ConflictDialog,
	tags: ['autodocs'],
	args: {
		onkeep_mine: fn(),
		ontake_theirs: fn(),
		onmerge_tags: fn()
	}
} satisfies Meta<typeof ConflictDialog>;

export default meta;
type Story = StoryObj<typeof meta>;

/** One field edited on two devices: a single diff row. */
export const SingleField: Story = {
	args: {
		current: serverTrack({ rating: 4 }),
		mine: { rating: 2 }
	}
};

/** Several fields differ, including the tag list that Merge tags is for. */
export const MultipleFields: Story = {
	args: {
		current: serverTrack({ rating: 4, bpm: 125, tags: ['peak', 'vocal', 'sunrise'] }),
		mine: { rating: 2, bpm: 124, tags: ['peak', 'vocal', 'warmup'], notes: 'check the drop' }
	}
};

/** Title missing on the server copy: the heading falls back to the stable id. */
export const UntitledTrack: Story = {
	args: {
		current: serverTrack({ title: null, rating: 1 }),
		mine: { rating: 5 }
	}
};

/** No edited keys at all: an empty diff table, buttons still present. */
export const EmptyDiff: Story = {
	args: {
		current: serverTrack(),
		mine: {}
	}
};
