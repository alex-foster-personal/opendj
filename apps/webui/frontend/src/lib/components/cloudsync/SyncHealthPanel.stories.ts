import type { Meta, StoryObj } from '@storybook/sveltekit';

import type { SyncHealth, SyncRun } from '$lib/api-cloudsync';

import SyncHealthPanel from './SyncHealthPanel.svelte';

const HUB = 'http://box.example-tailnet.ts.net:8686';

const lastRun = (over: Partial<SyncRun> = {}): SyncRun => ({
	started_at: '2026-11-05T11:59:00Z',
	finished_at: '2026-11-05T12:00:00Z',
	outcome: 'success',
	hub_url: HUB,
	consecutive_failures: 0,
	hub_machine_id: 'hub-machine-01',
	error_kind: null,
	error_message: null,
	pushed: 5,
	accepted: 5,
	rejected: 0,
	pulled: 2,
	applied: 2,
	rounds: 1,
	digest: null,
	hub_restore_detected: false,
	digest_inconclusive: false,
	hub_quarantined: null,
	quarantined_incoming: 0,
	...over
});

const health = (over: Partial<SyncHealth> = {}): SyncHealth => ({
	verdict: 'converged',
	local_seq: 42,
	pending_push_rows: 0,
	full_offer_pending: false,
	digest_mismatch: false,
	quarantined_rows: 0,
	peers: [
		{
			peer: 'hub-machine-01',
			last_push_seq: 40,
			last_pull_seq: 38,
			last_sync_at: '2026-11-05T12:00:00Z',
			peer_generation: 'gen-1',
			pending_push_rows: 0,
			full_offer_pending: false
		}
	],
	last_run: lastRun(),
	...over
});

/**
 * Fleet sync readout on /cloudsync overview: props mirror GET
 * /api/v1/cloudsync/overview `.sync` (`FleetHealth.to_wire()`).
 */
const meta = {
	title: 'Components/CloudSync/SyncHealthPanel',
	component: SyncHealthPanel,
	tags: ['autodocs'],
	args: {
		health: health()
	}
} satisfies Meta<typeof SyncHealthPanel>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Last sync settled; hub watermarks match local writes. */
export const Converged: Story = {};

/** Local changelog ahead of the hub push fence. */
export const Behind: Story = {
	args: {
		health: health({
			verdict: 'behind',
			pending_push_rows: 3,
			peers: [
				{
					peer: 'hub-machine-01',
					last_push_seq: 39,
					last_pull_seq: 38,
					last_sync_at: '2026-11-04T12:00:00Z',
					peer_generation: 'gen-1',
					pending_push_rows: 3,
					full_offer_pending: false
				}
			]
		})
	}
};

/** No peer watermark yet; pending push is n/a not zero. */
export const NeverSynced: Story = {
	args: {
		health: health({
			verdict: 'never_synced',
			local_seq: 0,
			pending_push_rows: null,
			peers: [],
			last_run: null
		})
	}
};

/** Repeated failed attempts with the last error surfaced. */
export const Failing: Story = {
	args: {
		health: health({
			verdict: 'failing',
			last_run: lastRun({
				consecutive_failures: 3,
				outcome: 'error',
				error_kind: 'ConnectError',
				error_message: 'hub unreachable'
			})
		})
	}
};
