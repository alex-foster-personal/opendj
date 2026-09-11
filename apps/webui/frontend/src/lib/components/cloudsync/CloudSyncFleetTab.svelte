<script lang="ts">
	/**
	 * /cloudsync Fleet tab: enrollment state of every machine this state DB
	 * knows. HTTP twin of `python -m apps.sync_hub fleet --json`
	 * (GET /api/v1/cloudsync/fleet, local operator only).
	 */
	import { onMount } from 'svelte';

	import { getCloudSyncFleet, type FleetOut } from '$lib/api-cloudsync-ops';

	let fleet = $state<FleetOut | null>(null);
	let loadError = $state<string | null>(null);

	async function refresh(): Promise<void> {
		try {
			fleet = await getCloudSyncFleet();
			loadError = null;
		} catch (exc) {
			loadError = exc instanceof Error ? exc.message : String(exc);
		}
	}

	onMount(refresh);
</script>

<section aria-label="CloudSync fleet" data-testid="cloudsync-fleet">
	<div class="head">
		<button type="button" title="Re-read the fleet from this machine's state DB" onclick={() => refresh()}>
			Refresh
		</button>
	</div>
	{#if loadError !== null}
		<p class="err" role="alert">Failed to load the fleet: {loadError}</p>
	{:else if fleet === null}
		<p class="muted">Loading fleet...</p>
	{:else}
		<p class="counts">
			<span title="Machines enrolled to an owner through a redeemed grant or credential">
				owned {fleet.owned}
			</span>
			<span title="Machines seen in the registry with no enrollment record (OBSERVE mode lets them sync)">
				unowned {fleet.unowned}
			</span>
			<span title="Machines enrolled by a different hub than the one this DB syncs with">
				foreign {fleet.foreign}
			</span>
			<span class="muted" title="The hub machine id this fleet readout is anchored to">
				hub {fleet.hub_machine_id}
			</span>
		</p>
		{#if fleet.machines.length === 0}
			<p class="muted">No machines registered yet.</p>
		{:else}
			<div class="table-scroll">
				<table class="library">
					<thead>
						<tr>
							<th>Machine</th>
							<th>State</th>
							<th>Owner</th>
							<th>Enrolled</th>
							<th>Via</th>
						</tr>
					</thead>
					<tbody>
						{#each fleet.machines as m (m.machine_id)}
							<tr data-testid="cloudsync-fleet-row" data-machine-name={m.name}>
								<td title={m.machine_id}>{m.name}</td>
								<td>{m.state}</td>
								<td>{m.owner_email ?? '-'}</td>
								<td title="UTC enrollment time">{m.enrolled_at ?? '-'}</td>
								<td>{m.enrolled_via ?? '-'}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	{/if}
</section>

<style>
	.head {
		display: flex;
		justify-content: flex-end;
		margin-bottom: 0.5rem;
	}
	.counts {
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
		font-size: 0.85rem;
	}
	.table-scroll {
		overflow-x: auto;
	}
	button {
		background: var(--bg);
		color: var(--fg);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 4px 8px;
		cursor: pointer;
	}
	.muted {
		color: var(--muted);
	}
	.err {
		color: var(--danger);
	}
</style>
