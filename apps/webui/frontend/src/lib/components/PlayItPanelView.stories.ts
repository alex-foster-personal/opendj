import type { Meta, StoryObj } from '@storybook/sveltekit';
import type { ComponentProps } from 'svelte';
import { fn } from 'storybook/test';

import PlayItPanelView from './PlayItPanelView.svelte';

type Props = ComponentProps<typeof PlayItPanelView>;
type Track = Props['playlistTracks'][number];
type Solve = NonNullable<Props['result']>;
type Step = Solve['steps'][number];

/** Invented playlist rows; only the fields the panel reads matter. */
const track = (n: number, title: string, artist: string | null): Track =>
	({ stable_id: String(n).repeat(40).slice(0, 40), title, artist }) as Track;

const tracks: Track[] = [
	track(1, 'Quiet Kiln', 'Lumen Fields'),
	track(2, 'Night Drive (Extended Mix)', 'Lumen Fields'),
	track(3, 'Low Tide', null),
	track(4, 'Sunrise Closer', 'Quiet Atlas')
];

const step = (
	position: number,
	t: Track,
	hint: string,
	pin_role: Step['pin_role'] = null
): Step => ({
	position,
	stable_id: t.stable_id,
	title: t.title,
	artist: t.artist,
	pin_role,
	transition_hint: hint,
	bpm: 124,
	bpm_delta_pct: 0,
	camelot_distance: 1,
	energy: 6,
	key_camelot: '8A',
	actual_energy: 6,
	target_energy: 6
});

const solve = (over: Partial<Solve> = {}): Solve => ({
	playlist_id: 'pl-0001',
	etag: 'W/"7"',
	solve_ms: 42,
	unchanged: false,
	previous_order: tracks.map((t) => t.stable_id),
	proposed_order: [tracks[2], tracks[0], tracks[1], tracks[3]].map((t) => t.stable_id),
	steps: [
		step(1, tracks[2], 'open low and wide', 'opener'),
		step(2, tracks[0], '+1 camelot, same tempo'),
		step(3, tracks[1], 'energy up to the peak', 'peak'),
		step(4, tracks[3], 'wind down', 'closer')
	],
	constraints_unmet: [],
	...over
});

/**
 * PLAY IT on a playlist page: set a duration, an optional peak time and
 * pins, solve an order, then apply or undo it. The container loads the
 * playlist and makes every request; these args are its states.
 */
const meta = {
	title: 'Components/PlayItPanelView',
	component: PlayItPanelView,
	tags: ['autodocs'],
	args: {
		durationMin: 60,
		peakAtMin: null,
		peakPins: [],
		openerPins: [],
		closerPin: '',
		playlistTracks: tracks,
		solving: false,
		solveError: null,
		result: null,
		applying: false,
		applyError: null,
		applied: null,
		solve: fn(),
		apply: fn(),
		undo: fn()
	}
} satisfies Meta<typeof PlayItPanelView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The goal form before a solve. */
export const Goal: Story = {};

/** Pins set and a solve in flight. */
export const Solving: Story = {
	args: {
		peakAtMin: 40,
		peakPins: [tracks[1].stable_id],
		closerPin: tracks[3].stable_id,
		solving: true
	}
};

/** A proposed order with pinned roles. */
export const Proposed: Story = { args: { result: solve() } };

/** The solver could not meet every constraint. */
export const ConstraintsUnmet: Story = {
	args: {
		result: solve({
			constraints_unmet: [
				{ kind: 'peak_at', position: 3, detail: {} }
			] as Solve['constraints_unmet']
		})
	}
};

/** The current order already matches. */
export const Unchanged: Story = {
	args: { result: solve({ proposed_order: tracks.map((t) => t.stable_id) }) }
};

/** Applied: undo is offered. */
export const Applied: Story = {
	args: {
		result: solve(),
		applied: { previousOrder: tracks.map((t) => t.stable_id), undoEtag: 'W/"8"' }
	}
};

/** The solver refused the goal. */
export const SolveError: Story = { args: { solveError: 'duration_min must be at least 30' } };

/** The playlist changed elsewhere before apply. */
export const ApplyConflict: Story = {
	args: {
		applyError: 'Playlist changed elsewhere since you solved -- re-run PLAY IT and try again.'
	}
};
