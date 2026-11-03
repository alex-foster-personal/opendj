import type { Meta, StoryObj } from '@storybook/sveltekit';
import { createRawSnippet } from 'svelte';
import { fn } from 'storybook/test';

import type { CloudSyncStatus } from '$lib/api-cloudsync';
import {
	chipAriaLabel,
	chipFullLabel,
	chipGlyphs,
	chipIcon,
	chipShortLabel,
	chipState,
	chipTitle
} from '$lib/components/cloudsync/cloudsync-view';

import CloudSyncStatusChipView from './CloudSyncStatusChipView.svelte';

/** An invented status in the shape of GET /api/v1/cloudsync/status. */
const status = (over: Partial<CloudSyncStatus> = {}): CloudSyncStatus => ({
	enabled: true,
	configured: true,
	running: true,
	heartbeat_at: '2026-10-14T21:04:00Z',
	enabled_source: 'file',
	endpoint_source: 'file',
	reason: null,
	signed_in_as: 'dj@example.com',
	last_push_at: '2026-10-14T21:03:40Z',
	last_pull_at: '2026-10-14T21:03:41Z',
	last_result: { status: 'ok', message: 'pushed 12, pulled 3' },
	rows_pending: 0,
	hash_pending: 0,
	quarantined: 0,
	excluded_total: 0,
	endpoint: 'http://box.example-tailnet.ts.net:8686',
	recent_results: [],
	update_required: null,
	...over
});

/**
 * Every label is derived by the app's own cloudsync-view rules from one
 * status, exactly as the container derives them; the args differ only in
 * that status. The quick-actions popover is a container in the app, so here
 * it is a placeholder snippet.
 */
const chipArgs = (s: CloudSyncStatus | null, loadError: string | null = null) => ({
	chipState: () => chipState(s),
	icon: chipIcon(s),
	glyphs: chipGlyphs(s),
	fullLabel: chipFullLabel(s),
	shortLabel: chipShortLabel(s),
	title: chipTitle(s, loadError),
	ariaLabel: chipAriaLabel(s, loadError)
});

const popover = createRawSnippet(() => ({
	render: () =>
		'<div style="position:absolute;top:28px;left:0;padding:8px;border:1px dashed var(--rb-border);font-size:12px;white-space:nowrap">CloudSyncQuickActions</div>'
}));

const meta = {
	title: 'Components/CloudSyncStatusChipView',
	component: CloudSyncStatusChipView,
	tags: ['autodocs'],
	args: { ...chipArgs(status()), popoverOpen: false, togglePopover: fn(), popover }
} satisfies Meta<typeof CloudSyncStatusChipView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Working: a cloud and a tick, in the shell foreground. */
export const Ok: Story = {};

/** Running, last attempt failed. */
export const Failing: Story = {
	args: chipArgs(status({ last_result: { status: 'error', message: 'hub refused the push' } }))
};

/** Never turned on: the neutral slashed cloud. */
export const NeverConfigured: Story = {
	args: chipArgs(status({ enabled: false, configured: false, running: false, last_result: null }))
};

/** Turned on but no scheduler heartbeat: amber with the "!". */
export const Stalled: Story = {
	args: chipArgs(status({ enabled: false, running: false, heartbeat_at: null }))
};

/** The peer speaks a newer wire version. */
export const UpdateRequired: Story = {
	args: chipArgs(
		status({
			update_required: {
				code: 'SYNC_WIRE_VERSION',
				local_wire_version: 3,
				peer_wire_version: 4,
				action: 'Update Open DJ on this machine.'
			}
		})
	)
};

/** Still loading. */
export const Loading: Story = { args: chipArgs(null) };

/** The status read failed. */
export const Unavailable: Story = { args: chipArgs(null, 'network error') };

/** The popover open below the chip. */
export const PopoverOpen: Story = { args: { popoverOpen: true } };
