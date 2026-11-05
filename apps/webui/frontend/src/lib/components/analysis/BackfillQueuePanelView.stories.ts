import type { Meta, StoryObj } from '@storybook/sveltekit';
import type { ComponentProps } from 'svelte';
import { fn } from 'storybook/test';

import { BAND_LABELS } from '$lib/rb/api-analysis-backfill';

import BackfillQueuePanelView from './BackfillQueuePanelView.svelte';

type Props = ComponentProps<typeof BackfillQueuePanelView>;
type Progress = NonNullable<Props['watched']>;
type Plan = NonNullable<Props['lastPlan']>;

const model: Plan['memory_model'] = {
	backend: 'own_beatgrid.backfill',
	producer_version: 'own_beatgrid 0.4.1',
	floor_mb: 410,
	slope_mb_per_min: 38.5,
	measured_on: '2026-10-02 on a 16 GB laptop',
	source: 'docs/perf/measurements/beatgrid-rss.md'
};

const counts = (over: Partial<Progress['counts']> = {}): Progress['counts'] => ({
	pending: 0,
	running: 0,
	done: 0,
	skipped: 0,
	failed: 0,
	refused: 0,
	cancelled: 0,
	...over
});

const plan: Plan = {
	batch_id: 'bf-20261014-0001',
	offered: 5,
	admitted: 4,
	refused: 1,
	workers: 2,
	band: '20_to_45_min',
	memory_model: model
};

const watched: Progress = {
	batch_id: plan.batch_id,
	state: 'running',
	workers: 2,
	band: '20_to_45_min',
	memory_model: model,
	counts: counts({ pending: 1, running: 2, done: 1, refused: 1 }),
	total: 5,
	settled: 2,
	created_at: '2026-10-14T20:00:00Z',
	updated_at: '2026-10-14T20:06:00Z',
	items: [
		['a', 'done', null, 1],
		['b', 'running', null, 1],
		['c', 'running', null, 2],
		['d', 'pending', null, 0],
		['e', 'refused', 'longer than 90 minutes (112 min)', 0]
	].map(([id, state, reason, attempts]) => ({
		stable_id: String(id).repeat(40),
		lane: 'beatgrid',
		backend: 'own_beatgrid.backfill',
		state: state as Progress['items'][number]['state'],
		reason: reason as string | null,
		attempts: attempts as number,
		duration_s: null,
		predicted_peak_mb: null
	}))
};

/**
 * The native-analysis backfill queue (NATIVE-10): plan, watch and steer a
 * batch, and copy the drain command. The container makes every request and
 * owns the two-second poll; these args are its states. Ids are invented.
 */
const meta = {
	title: 'Components/Analysis/BackfillQueuePanelView',
	component: BackfillQueuePanelView,
	tags: ['autodocs'],
	args: {
		stableIdsText: '',
		lane: 'beatgrid',
		backend: 'own_beatgrid.backfill',
		note: '',
		parsedIds: [],
		batches: [],
		watched: null,
		lastPlan: null,
		fault: null,
		busy: false,
		drainCommand: null,
		bandLabels: BAND_LABELS,
		onEnqueue: fn(),
		onCancel: fn(),
		onResume: fn(),
		refreshWatched: fn(),
		watch: fn()
	}
} satisfies Meta<typeof BackfillQueuePanelView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Nothing queued yet. */
export const Empty: Story = {};

/** The batch list has not loaded. */
export const NotLoaded: Story = { args: { batches: null } };

/** Ids typed in, ready to enqueue. */
export const ReadyToEnqueue: Story = {
	args: {
		stableIdsText: 'aaaa1111\nbbbb2222, cccc3333',
		parsedIds: ['aaaa1111', 'bbbb2222', 'cccc3333']
	}
};

/** A batch planned and watched: the plan, the drain command, progress and items. */
export const Watching: Story = {
	args: {
		lastPlan: plan,
		watched,
		drainCommand: `python -m apps.analysis.queue_cli run --batch-id ${plan.batch_id} --backend own_beatgrid.backfill`,
		batches: [
			{
				batch_id: plan.batch_id,
				state: 'running',
				workers: 2,
				band: '20_to_45_min',
				created_at: '2026-10-14T20:00:00Z',
				note: 'warehouse crate',
				counts: watched.counts
			},
			{
				batch_id: 'bf-20261013-0007',
				state: 'done',
				workers: 4,
				band: 'under_20_min',
				created_at: '2026-10-13T18:20:00Z',
				note: null,
				counts: counts({ done: 12 })
			}
		]
	}
};

/** A request in flight: every control disabled. */
export const Busy: Story = { args: { ...Watching.args, busy: true } };

/** The daemon answered with an error, shown verbatim. */
export const Fault: Story = { args: { fault: 'RbApiError: batch bf-20261014-0099 not found' } };
