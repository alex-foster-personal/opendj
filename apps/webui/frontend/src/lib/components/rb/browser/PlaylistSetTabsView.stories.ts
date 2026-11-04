import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import type { ComponentProps } from 'svelte';

import PlaylistSetTabsView from './PlaylistSetTabsView.svelte';

/**
 * Playlist sets (SET-05): named orderings of one playlist, as tabs with a
 * play count, a create box, and practice / perform controls once a set is
 * chosen. The container loads the sets and runs create and start. Set names
 * are invented.
 */
type PlaylistSet = ComponentProps<typeof PlaylistSetTabsView>['sets'][number];

const set = (id: number, name: string, play_count: number): PlaylistSet => ({
	id,
	playlist_id: 'pl-1',
	name,
	play_count,
	entries: [],
	parts: [],
	created_at: '2026-10-01T18:00:00Z',
	updated_at: '2026-10-10T18:00:00Z'
});

const sets = [set(1, 'Friday', 3), set(2, 'Long Set', 0), set(3, 'Rooftop Sunset', 12)];

const meta = {
	title: 'Components/Browser/PlaylistSetTabsView',
	component: PlaylistSetTabsView,
	tags: ['autodocs'],
	args: {
		sets,
		selectedId: null,
		mode: 'practice',
		newName: '',
		creating: false,
		running: false,
		oncreate: fn(),
		onrun: fn()
	},
	parameters: { layout: 'fullscreen' }
} satisfies Meta<typeof PlaylistSetTabsView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Sets listed, none chosen: no mode row. */
export const Tabs: Story = {};

/** A set chosen, practice mode pressed. */
export const Selected: Story = { args: { selectedId: 1 } };

/** Perform mode pressed. */
export const PerformMode: Story = { args: { selectedId: 3, mode: 'performance' } };

/** A run is starting: both start buttons disabled. */
export const Running: Story = { args: { selectedId: 1, running: true } };

/** A name typed, so '+ Set' is enabled. */
export const NameTyped: Story = { args: { newName: 'Warmup' } };

/** A practice run started: the button reads 'Practice on' and the status line shows the play count. */
export const RunLive: Story = {
	args: {
		selectedId: 1,
		runNote: 'Practice running. Play count 4.',
		runIsLive: (kind) => kind === 'practice',
		startLabel: (kind) => (kind === 'practice' ? 'Practice on' : 'Start performance')
	}
};

/** A run that failed: the status line turns red and names the error. */
export const RunFailed: Story = {
	args: {
		selectedId: 2,
		runNote: 'Could not start performance: the set run did not start',
		runError: true
	}
};

/** No sets yet: only the create box. */
export const Empty: Story = { args: { sets: [] } };
