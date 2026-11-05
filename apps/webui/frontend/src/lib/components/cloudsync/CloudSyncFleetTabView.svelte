<script lang="ts">
	/**
	 * Props-only half of CloudSyncFleetTab: the /cloudsync Fleet tab and its
	 * enrollment-grant form. The container owns the fetch, the grant request,
	 * the clipboard copy and the grant state; this file draws the states and
	 * reports each action as a callback.
	 */
	import type { FleetOut, GrantOut } from '$lib/api-cloudsync-ops';

	let {
		fleet,
		loadError,
		onRefresh,
		grantEmail = $bindable(''),
		grantBusy,
		grantError,
		grant,
		copyState,
		expiryTitle,
		enrollHint,
		onCreateGrant,
		onCopyToken
	}: {
		fleet: FleetOut | null;
		loadError: string | null;
		onRefresh: () => void;
		grantEmail: string;
		grantBusy: boolean;
		grantError: string | null;
		grant: GrantOut | null;
		copyState: 'idle' | 'copied' | 'failed';
		/** The grant's expiry tooltip, computed by the container; '' when no grant. */
		expiryTitle: string;
		/** The one-line enroll command shown beside the token. */
		enrollHint: string;
		onCreateGrant: () => void;
		onCopyToken: () => void;
	} = $props();
</script>

<section aria-label="CloudSync fleet" data-testid="cloudsync-fleet">
	<div class="head">
		<button
			type="button"
			title="Re-read the fleet from this machine's state DB"
			onclick={() => onRefresh()}
		>
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
			<span
				title="Machines seen in the registry with no enrollment record (OBSERVE mode lets them sync)"
			>
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

	<form
		class="grant"
		data-testid="cloudsync-grant-form"
		aria-label="Create enrollment grant"
		onsubmit={(e) => {
			e.preventDefault();
			onCreateGrant();
		}}
	>
		<h3>Add a machine</h3>
		<p class="muted">
			Create a single-use enrollment grant on this hub for a user who has already signed in here,
			then redeem it on the joining machine.
		</p>
		<div class="grant-row">
			<input
				type="email"
				autocomplete="off"
				placeholder="owner email"
				title="Email of a user who has signed in on this hub; the new machine is enrolled under them"
				bind:value={grantEmail}
				disabled={grantBusy}
			/>
			<button
				type="submit"
				disabled={grantBusy}
				title="Mint one single-use enrollment grant on this hub (python -m apps.sync_hub grant)"
			>
				{grantBusy ? 'Creating...' : 'Create enrollment grant'}
			</button>
		</div>
		{#if grantError !== null}
			<p class="err" role="alert" data-testid="cloudsync-grant-error">{grantError}</p>
		{/if}
		{#if grant !== null}
			<div class="grant-out" data-testid="cloudsync-grant-out">
				<p>
					Grant for {grant.owner_email}. Shown once: copy it now.
					<span title={expiryTitle}>Expires {grant.expires_at}.</span>
				</p>
				<div class="grant-row">
					<input
						class="token"
						type="text"
						readonly
						aria-label="Enrollment grant token"
						title="Single-use enrollment token; only its hash is stored on the hub"
						value={grant.token}
					/>
					<button
						type="button"
						title="Copy the token to the clipboard"
						onclick={() => onCopyToken()}
					>
						{copyState === 'copied' ? 'Copied' : 'Copy'}
					</button>
				</div>
				{#if copyState === 'failed'}
					<p class="err" role="alert">Could not copy; select the token and copy it by hand.</p>
				{/if}
				<p class="muted">
					On the joining machine, run <code>{enrollHint}</code> and paste the token on stdin.
				</p>
			</div>
		{/if}
	</form>
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
	.grant {
		margin-top: 1.5rem;
		border-top: 1px solid var(--border);
		padding-top: 0.75rem;
	}
	.grant h3 {
		margin: 0 0 0.25rem;
		font-size: 0.95rem;
	}
	.grant-row {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		align-items: center;
	}
	.grant input {
		background: var(--bg);
		color: var(--fg);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 4px 8px;
		min-width: 16em;
	}
	.grant input.token {
		font-family: monospace;
		flex: 1;
	}
	.grant code {
		word-break: break-all;
	}
	.muted {
		color: var(--muted);
	}
	.err {
		color: var(--danger);
	}
</style>
