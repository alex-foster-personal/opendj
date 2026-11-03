import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import CloudSyncPolicyCellView from './CloudSyncPolicyCellView.svelte';

/**
 * One cell of the /cloudsync policy matrix. The container derives mode,
 * budget state and titles from the stored policy and owns the save; each
 * change is reported as its raw value. The cell is a <td> laid out as a
 * flex row, so it draws the same outside a table.
 */
const meta = {
	title: 'Components/CloudSync/CloudSyncPolicyCellView',
	component: CloudSyncPolicyCellView,
	tags: ['autodocs'],
	args: {
		assetKind: 'audio',
		kindLabel: 'Audio',
		machineName: 'gig-laptop',
		unset: 'unset',
		unsetOptionTitle:
			'Unset: no policy is stored for this cell, so this machine keeps this kind local-only. Picking unset on a stored cell clears it with a synced tombstone.',
		modes: ['pinned', 'cached', 'stream', 'excluded'],
		mode: 'cached',
		budgetEditable: true,
		budgetTitle: 'Local hot-cache budget in MB (LRU eviction above this).',
		budgetValue: 2048,
		onModeChange: fn(),
		onBudgetChange: fn()
	}
} satisfies Meta<typeof CloudSyncPolicyCellView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Cached with a budget: the only mode where the budget input is live. */
export const Cached: Story = {};

/** A non-cached mode: the budget input is disabled and says why. */
export const Pinned: Story = {
	args: {
		mode: 'pinned',
		budgetEditable: false,
		budgetTitle: 'Cache budget only applies in cached mode (stored mode is pinned).',
		budgetValue: ''
	}
};

/** No stored row: the select shows the italic unset option. */
export const Unset: Story = {
	args: {
		mode: 'unset',
		budgetEditable: false,
		budgetTitle: 'No policy is stored for this cell. Pick a mode first; a budget applies only in cached mode.',
		budgetValue: ''
	}
};
