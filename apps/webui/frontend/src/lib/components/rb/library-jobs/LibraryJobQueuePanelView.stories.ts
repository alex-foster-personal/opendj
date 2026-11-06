import type { Meta, StoryObj } from '@storybook/sveltekit';

import type { ComponentProps } from 'svelte';
import { fn } from 'storybook/test';

import LibraryJobQueuePanelView from './LibraryJobQueuePanelView.svelte';

/**
 * The library job queue panel (PERFBATCH-05): one lane at a time (stems or
 * lyrics), the running job first, then queued jobs numbered in queue order
 * and draggable to reorder, each with Cancel. The panel is absolutely
 * positioned over the bottom bar, so the decorator gives the root that
 * frame. Track ids are invented.
 */
type Item = ComponentProps<typeof LibraryJobQueuePanelView>['running'][number];

const item = (
	stable_id: string,
	state: Item['state'],
	position: number,
	lane: Item['lane'] = 'stems'
): Item => ({
	lane,
	stable_id,
	state,
	reason: null,
	detail: null,
	position,
	attempts: state === 'running' ? 1 : 0,
	enqueued_at: '2026-10-14T21:00:00+00:00',
	started_at: state === 'running' ? '2026-10-14T21:01:00+00:00' : null,
	finished_at: null
});

const meta = {
	title: 'Components/LibraryJobs/LibraryJobQueuePanelView',
	component: LibraryJobQueuePanelView,
	tags: ['autodocs'],
	decorators: [
		(story) => {
			const root =
				typeof document !== 'undefined' ? document.getElementById('storybook-root') : null;
			if (root) {
				root.style.position = 'relative';
				root.style.width = '340px';
				root.style.height = '340px';
			}
			return story();
		}
	],
	args: {
		open: true,
		lane: 'stems',
		error: null,
		running: [item('trk-0042', 'running', 0)],
		pending: [
			item('trk-0107', 'pending', 1),
			item('trk-0233', 'pending', 2),
			item('trk-0318', 'pending', 3)
		],
		onSelectLane: fn(),
		onClose: fn(),
		onCancel: fn(),
		onDragStart: fn(),
		onDrop: fn()
	}
} satisfies Meta<typeof LibraryJobQueuePanelView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Stems lane: one running job, three queued in order. */
export const StemsQueue: Story = {};

/** Lyrics lane selected, a single queued job. */
export const LyricsLane: Story = {
	args: { lane: 'lyrics', running: [], pending: [item('trk-0510', 'pending', 1, 'lyrics')] }
};

/** Nothing queued or running in this lane. */
export const Empty: Story = { args: { running: [], pending: [] } };

/** A cancel or reorder call failed: the reason shows above the list. */
export const CallFailed: Story = { args: { error: 'reorder failed: 409 job already running' } };

/** Closed: nothing is drawn. */
export const Closed: Story = { args: { open: false }, tags: ['renders-nothing'] };
