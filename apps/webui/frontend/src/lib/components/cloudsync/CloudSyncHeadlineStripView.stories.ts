import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import CloudSyncHeadlineStripView from './CloudSyncHeadlineStripView.svelte';

const post = {
	kind: 'post',
	body: { hub_url: 'http://box.example-tailnet.ts.net:8686', name: 'gig-laptop', force: false }
} as const;

/**
 * Shared headline strip above every /cloudsync tab (issue #8720). Labels are
 * args because cloudsync-view.ts reaches the backend; only types are imported
 * in the View.
 */
const meta = {
	title: 'Components/CloudSync/CloudSyncHeadlineStripView',
	component: CloudSyncHeadlineStripView,
	tags: ['autodocs'],
	args: {
		headline: { tone: 'ok' as const, text: 'In sync. Last synced 4m ago.' },
		action: 'sync-now' as const,
		syncNowLabel: 'Sync now',
		refreshLabel: 'Refresh',
		setUpLabel: 'Set up →',
		syncing: false,
		refreshing: false,
		gateLoadError: null,
		syncDecision: post,
		notice: null,
		onSyncNow: fn(),
		onRefresh: fn(),
		onSetUp: fn()
	}
} satisfies Meta<typeof CloudSyncHeadlineStripView>;

export default meta;
type Story = StoryObj<typeof meta>;

export const InSync: Story = {};

export const Syncing: Story = {
	args: {
		headline: { tone: 'warn', text: 'Syncing...' },
		action: 'refresh'
	}
};

export const NotSetUp: Story = {
	args: {
		headline: {
			tone: 'off',
			text: 'CloudSync is not set up on this machine. Enter a hub URL below and save to turn it on.'
		},
		action: 'set-up'
	}
};

export const Error: Story = {
	args: {
		headline: {
			tone: 'error',
			text: 'Not synced: could not reach the hub machine (connection refused). Check the hub URL below and that the hub machine is running, then try Sync now again.'
		},
		action: 'sync-now'
	}
};

export const ManualOnly: Story = {
	args: {
		headline: {
			tone: 'off',
			text: 'Automatic sync is off, but a hub URL is saved (http://box.example-tailnet.ts.net:8686). Use Sync now below, or turn on automatic sync above.'
		},
		action: 'sync-now'
	}
};

export const Loading: Story = {
	args: {
		headline: { tone: 'off', text: 'Loading CloudSync status...' },
		action: 'refresh'
	}
};
