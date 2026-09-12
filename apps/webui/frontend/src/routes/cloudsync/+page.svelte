<script lang="ts">
	/**
	 * CloudSync config surface (specs/cloudsync-spec.md D5): per-machine
	 * asset policy matrix, playlist pins, and a fleet hydration overview.
	 * Every control here is backed live by /api/v1/cloudsync/* -- full
	 * agent parity, nothing UI-only.
	 */
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';

	import {
		ASSET_KINDS,
		SYNC_MODES,
		getOverview,
		listMachines,
		listPlaylistPins,
		listPolicies,
		putPlaylistPin,
		type AssetKind,
		type CloudMachine,
		type CloudSyncOverview,
		type PlaylistPin,
		type SyncMode,
		type SyncPolicy
	} from '$lib/api-cloudsync';
	import CloudSyncFleetTab from '$lib/components/cloudsync/CloudSyncFleetTab.svelte';
	import CloudSyncPolicyCell from '$lib/components/cloudsync/CloudSyncPolicyCell.svelte';
	import CloudSyncStatusTab from '$lib/components/cloudsync/CloudSyncStatusTab.svelte';
	import {
		CLOUDSYNC_TABS,
		cloudSyncTabFromUrl,
		inertKindTitle
	} from '$lib/components/cloudsync/cloudsync-view';
	import { listPlaylistsHydrated, type PlaylistSummaryHydrated } from '$lib/rb/api-rb';
	import { pushToast } from '$lib/stores.svelte';

	const TABS = CLOUDSYNC_TABS;

	const ASSET_KIND_LABEL: Record<AssetKind, string> = {
		audio: 'Audio',
		stem_bundle: 'Stem bundles',
		anlz_cache: 'ANLZ cache',
		vocal_cache: 'Vocal cache',
		lyrics_cache: 'Lyrics cache',
		karaoke_words: 'Karaoke word timings'
	};

	const tab = $derived(cloudSyncTabFromUrl($page.url));
	let loading = $state(true);
	let loadError = $state<string | null>(null);

	let machines = $state<CloudMachine[]>([]);
	let policies = $state<SyncPolicy[]>([]);
	let pins = $state<PlaylistPin[]>([]);
	let overview = $state<CloudSyncOverview | null>(null);
	let playlists = $state<PlaylistSummaryHydrated[]>([]);

	let newPinMachineId = $state('');
	let newPinPlaylistId = $state('');
	let newPinMode = $state<SyncMode>('pinned');
	let pinBusy = $state(false);

	function selectTab(next: (typeof TABS)[number]['id']): void {
		const url = new URL($page.url);
		url.searchParams.set('tab', next);
		void goto(url, { replaceState: true, keepFocus: true, noScroll: true });
	}

	function policyFor(machineId: string, assetKind: AssetKind): SyncPolicy | undefined {
		return policies.find((p) => p.machine_id === machineId && p.asset_kind === assetKind);
	}

	function machineName(machineId: string): string {
		return machines.find((m) => m.machine_id === machineId)?.name ?? machineId;
	}

	function upsertPolicy(saved: SyncPolicy): void {
		policies = [
			...policies.filter(
				(p) => !(p.machine_id === saved.machine_id && p.asset_kind === saved.asset_kind)
			),
			saved
		];
	}

	function upsertPin(saved: PlaylistPin): void {
		pins = [
			...pins.filter(
				(p) => !(p.machine_id === saved.machine_id && p.playlist_id === saved.playlist_id)
			),
			saved
		];
	}

	function errorMessage(exc: unknown): string {
		return exc instanceof Error ? exc.message : String(exc);
	}

	async function reloadCore(): Promise<void> {
		const [m, p, pn, ov] = await Promise.all([
			listMachines(),
			listPolicies(),
			listPlaylistPins(),
			getOverview()
		]);
		machines = m;
		policies = p;
		pins = pn;
		overview = ov;
		if (newPinMachineId === '' && machines.length > 0) {
			newPinMachineId = machines[0].machine_id;
		}
	}

	onMount(async () => {
		try {
			const [, pls] = await Promise.all([reloadCore(), listPlaylistsHydrated()]);
			playlists = pls;
			if (newPinPlaylistId === '' && playlists.length > 0) {
				newPinPlaylistId = playlists[0].playlist_id;
			}
		} catch (exc) {
			loadError = errorMessage(exc);
			pushToast(`Failed to load CloudSync: ${loadError}`, 'error');
		} finally {
			loading = false;
		}
	});

	async function addPin(): Promise<void> {
		if (!newPinMachineId || !newPinPlaylistId) {
			pushToast('Pick a machine and a playlist first', 'error');
			return;
		}
		pinBusy = true;
		try {
			const saved = await putPlaylistPin({
				machine_id: newPinMachineId,
				playlist_id: newPinPlaylistId,
				mode: newPinMode
			});
			upsertPin(saved);
			pushToast(
				`${saved.playlist_name ?? saved.playlist_id} set to ${saved.mode} on ${machineName(saved.machine_id)}`
			);
		} catch (exc) {
			pushToast(`Failed to save playlist pin: ${errorMessage(exc)}`, 'error');
		} finally {
			pinBusy = false;
		}
	}

	async function onPinModeChange(pin: PlaylistPin, mode: SyncMode): Promise<void> {
		try {
			const saved = await putPlaylistPin({
				machine_id: pin.machine_id,
				playlist_id: pin.playlist_id,
				mode
			});
			upsertPin(saved);
		} catch (exc) {
			pushToast(`Failed to update pin: ${errorMessage(exc)}`, 'error');
		}
	}
</script>

<h2>CloudSync</h2>
<p style="color: var(--muted);">
	Per-machine sync policy over R2 (specs/cloudsync-spec.md D5). Machines self-register the first
	time this page loads on them or they sync with the hub; the Status tab shows whether this
	machine's background scheduler is actually running.
</p>

<div class="tabbar" role="tablist" aria-label="CloudSync sections">
	{#each TABS as t (t.id)}
		<button
			type="button"
			role="tab"
			aria-selected={tab === t.id}
			class:on={tab === t.id}
			onclick={() => selectTab(t.id)}
		>
			{t.label}
		</button>
	{/each}
</div>

{#if tab === 'status'}
	<CloudSyncStatusTab />
{:else if tab === 'fleet'}
	<CloudSyncFleetTab />
{:else if loading}
	<p>Loading...</p>
{:else if loadError}
	<p style="color: var(--danger);">Failed to load CloudSync: {loadError}</p>
{:else if tab === 'policies'}
	<section aria-label="Machines and asset policy">
		{#if machines.length === 0}
			<p style="color: var(--muted);">No machines registered yet.</p>
		{:else}
			<div class="table-scroll">
				<table class="library matrix">
					<thead>
						<tr>
							<th>Machine</th>
							{#each ASSET_KINDS as kind (kind)}
								{@const inert = inertKindTitle(kind)}
								<th title={inert ?? undefined} class:inert={inert !== null}>
									{ASSET_KIND_LABEL[kind]}{#if inert !== null}<span class="chip">inert</span>{/if}
								</th>
							{/each}
						</tr>
					</thead>
					<tbody>
						{#each machines as m (m.machine_id)}
							<tr>
								<td
									title={`platform ${m.platform}${m.is_hub ? ', hub' : ''}; last seen ${m.last_seen}`}
								>
									{m.name}{#if m.is_hub}<span class="chip">hub</span>{/if}
								</td>
								{#each ASSET_KINDS as kind (kind)}
									<CloudSyncPolicyCell
										machineId={m.machine_id}
										machineName={m.name}
										assetKind={kind}
										kindLabel={ASSET_KIND_LABEL[kind]}
										policy={policyFor(m.machine_id, kind)}
										onSaved={upsertPolicy}
										onError={(message) => pushToast(message, 'error')}
									/>
								{/each}
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</section>
{:else if tab === 'pins'}
	<section aria-label="Playlist pins">
		<div class="pin-form">
			<select
				title="Machine to pin a playlist for"
				bind:value={newPinMachineId}
				disabled={machines.length === 0}
			>
				{#each machines as m (m.machine_id)}
					<option value={m.machine_id}>{m.name}</option>
				{/each}
			</select>
			<select
				title="Playlist to pin"
				bind:value={newPinPlaylistId}
				disabled={playlists.length === 0}
			>
				{#each playlists as pl (pl.playlist_id)}
					<option value={pl.playlist_id}>{pl.name}</option>
				{/each}
			</select>
			<select title="Sync mode for this pin" bind:value={newPinMode}>
				{#each SYNC_MODES as mode (mode)}
					<option value={mode}>{mode}</option>
				{/each}
			</select>
			<button
				type="button"
				disabled={pinBusy || !newPinMachineId || !newPinPlaylistId}
				title={!newPinMachineId || !newPinPlaylistId
					? 'Pick a machine and a playlist first'
					: 'Save this playlist pin'}
				onclick={() => addPin()}
			>
				{pinBusy ? 'Saving...' : 'Set pin'}
			</button>
		</div>

		{#if pins.length === 0}
			<p style="color: var(--muted);">No playlist pins yet.</p>
		{:else}
			<div class="table-scroll">
				<table class="library">
					<thead>
						<tr>
							<th>Machine</th>
							<th>Playlist</th>
							<th>Mode</th>
							<th>Updated</th>
						</tr>
					</thead>
					<tbody>
						{#each pins as pin (pin.machine_id + ':' + pin.playlist_id)}
							<tr>
								<td>{machineName(pin.machine_id)}</td>
								<td>{pin.playlist_name ?? pin.playlist_id}</td>
								<td>
									<select
										title={`Sync mode for ${pin.playlist_name ?? pin.playlist_id} on ${machineName(pin.machine_id)}`}
										value={pin.mode}
										onchange={(e) =>
											onPinModeChange(pin, (e.currentTarget as HTMLSelectElement).value as SyncMode)}
									>
										{#each SYNC_MODES as mode (mode)}
											<option value={mode}>{mode}</option>
										{/each}
									</select>
								</td>
								<td title={pin.updated_at ?? 'never updated'}>{pin.updated_at ?? '-'}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</section>
{:else if tab === 'overview' && overview}
	<section aria-label="Fleet overview">
		<p
			style="color: var(--muted);"
			title="Total rows in the tracks table, regardless of any machine's sync policy"
		>
			{overview.total_tracks} tracks in the library.
		</p>
		{#if overview.machines.length === 0}
			<p style="color: var(--muted);">No machines registered yet.</p>
		{:else}
			<div class="table-scroll">
				<table class="library">
					<thead>
						<tr>
							<th>Machine</th>
							<th>Pinned</th>
							<th>Cached</th>
							<th>Stream</th>
							<th>Unhydrated pinned</th>
							<th>Last sync</th>
						</tr>
					</thead>
					<tbody>
						{#each overview.machines as m (m.machine_id)}
							<tr>
								<td>{m.name}</td>
								<td
									title="Distinct tracks in playlists pinned (local, always-hydrated) to this machine"
								>
									{m.pinned_tracks}
								</td>
								<td title="Distinct tracks in playlists set to cached (LRU local copy) on this machine">
									{m.cached_tracks}
								</td>
								<td title="Distinct tracks in playlists set to stream-only (no local copy) on this machine">
									{m.stream_tracks}
								</td>
								<td
									class:warn={m.unhydrated_pinned_count > 0}
									title="Of this machine's pinned tracks, how many have no available local file yet (track_locations kind=local, available=1) -- these should hydrate before a gig"
								>
									{m.unhydrated_pinned_count}
								</td>
								<td title="Last hub-sync exchange with this machine's peer id, from the local sync_state table (blank until the hub-sync daemon runs)">
									{m.last_sync_at ?? 'never'}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</section>
{/if}

<style>
	.tabbar {
		display: flex;
		gap: 6px;
		margin: 0.5rem 0 1rem;
	}
	.tabbar button {
		padding: 6px 12px;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--muted);
		cursor: pointer;
		font-size: 0.85rem;
	}
	.tabbar button.on {
		color: var(--fg);
		border-color: var(--accent);
	}
	.table-scroll {
		overflow-x: auto;
	}
	th.inert {
		color: var(--muted);
	}
	select {
		background: var(--bg);
		color: var(--fg);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 3px 6px;
		font-size: 0.82rem;
	}
	.pin-form {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		align-items: center;
		margin-bottom: 1rem;
	}
	.pin-form button {
		padding: 6px 12px;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--fg);
		cursor: pointer;
	}
	.pin-form button:disabled {
		opacity: 0.5;
		cursor: default;
	}
	td.warn {
		color: var(--danger);
		font-weight: 600;
	}
</style>
