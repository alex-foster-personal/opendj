import type { Meta, StoryObj } from '@storybook/sveltekit';
import { createRawSnippet } from 'svelte';
import { fn } from 'storybook/test';

import LibraryJobsChromeView from './LibraryJobsChromeView.svelte';

/**
 * Bottom-bar library job chrome: the offload ribbon (filled to the busiest
 * job's progress, in that job kind's color; click opens the queue) and the
 * orange "awaiting Rekordbox import" chip, whose popover confirms each staged
 * batch. The queue panel is its own container in the app, so here it is an
 * empty `queuePanel` snippet (LibraryJobQueuePanelView has its own stories).
 * Batch names and folders are invented.
 */
const queuePanel = createRawSnippet(() => ({ render: () => '<span></span>' }));

const batch = (name: string) => ({ name, dest_dir: `/music/Music/Incoming/${name}` });

const meta = {
	title: 'Components/LibraryJobs/LibraryJobsChromeView',
	component: LibraryJobsChromeView,
	tags: ['autodocs'],
	parameters: { layout: 'padded' },
	decorators: [
		(story) => {
			const root =
				typeof document !== 'undefined' ? document.getElementById('storybook-root') : null;
			if (root) {
				root.style.display = 'flex';
				root.style.gap = '8px';
				root.style.alignItems = 'center';
				root.style.minHeight = '120px';
				root.style.alignContent = 'flex-start';
			}
			return story();
		}
	],
	args: {
		ribbon: { kind: 'stems', label: 'stems 1/3', progress: 0.4 },
		pendingBatches: [],
		rbPopoverOpen: false,
		onToggleQueue: fn(),
		onToggleRbPopover: fn(),
		onConfirmBatch: fn(),
		queuePanel
	}
} satisfies Meta<typeof LibraryJobsChromeView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A stems offload running: the ribbon fills to its progress. */
export const StemsRibbon: Story = {};

/** A lyrics job just queued: the ribbon shows its 2% floor so it never reads empty. */
export const JustQueued: Story = {
	args: { ribbon: { kind: 'lyrics', label: 'lyrics 0/1', progress: 0 } }
};

/** One staged batch awaiting Rekordbox import, beside the ribbon. */
export const AwaitingImport: Story = { args: { pendingBatches: [batch('2026-10-14-a')] } };

/** Two batches, popover open: one confirm button per batch, folder on hover. */
export const AwaitingImportOpen: Story = {
	args: {
		ribbon: null,
		pendingBatches: [batch('2026-10-14-a'), batch('2026-10-14-b')],
		rbPopoverOpen: true
	}
};

/** No job and nothing awaiting import: the chrome draws nothing. */
export const Idle: Story = { args: { ribbon: null }, tags: ['renders-nothing'] };
