<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import {
		applyWriteback,
		getWritebackCapabilities,
		getWritebackPlan,
		type VendorCapability,
		type WritebackApplyResult,
		type WritebackPlan,
		type WritebackVendor
	} from '$lib/rb/api-writeback';
	import { RbApiError } from '$lib/rb/api-rb';

	let playlistId = $state('');
	let capabilities = $state<VendorCapability[] | null>(null);
	let selectedVendor = $state<WritebackVendor | null>(null);
	let plan = $state<WritebackPlan | null>(null);
	let applyResult = $state<WritebackApplyResult | null>(null);
	let forceAdopt = $state(false);
	let error = $state<string | null>(null);
	let loading = $state(false);

	onMount(async () => {
		const id = $page.params.id;
		if (id === undefined) throw new Error('playlist route param "id" missing');
		playlistId = id;
		await refreshCapabilities();
	});

	async function refreshCapabilities() {
		error = null;
		try {
			const caps = await getWritebackCapabilities(playlistId);
			capabilities = caps.vendors;
			const firstAvailable = caps.vendors.find((v) => v.available);
			if (firstAvailable) {
				selectedVendor = firstAvailable.vendor as WritebackVendor;
				await refreshPlan();
			}
		} catch (e) {
			error = e instanceof RbApiError ? e.message : String(e);
		}
	}

	async function refreshPlan() {
		if (!selectedVendor) return;
		error = null;
		applyResult = null;
		loading = true;
		try {
			plan = await getWritebackPlan(playlistId, selectedVendor);
		} catch (e) {
			plan = null;
			error = e instanceof RbApiError ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	async function onVendorChange(vendor: WritebackVendor) {
		selectedVendor = vendor;
		forceAdopt = false;
		await refreshPlan();
	}

	async function onApply() {
		if (!selectedVendor) return;
		error = null;
		loading = true;
		try {
			applyResult = await applyWriteback(playlistId, selectedVendor, {
				dry_run: false,
				force_adopt: forceAdopt
			});
			if (applyResult.applied) await refreshPlan();
		} catch (e) {
			error = e instanceof RbApiError ? e.message : String(e);
		} finally {
			loading = false;
		}
	}
</script>

<a href={`/playlist/${playlistId}`}>&larr; back</a>
<h2>Write back to rekordbox / djay</h2>

{#if error}
	<p style="color: var(--error, #c0392b);">{error}</p>
{/if}

{#if capabilities}
	<div class="vendor-picker">
		{#each capabilities as cap (cap.vendor)}
			<label>
				<input
					type="radio"
					name="vendor"
					value={cap.vendor}
					disabled={!cap.available}
					checked={selectedVendor === cap.vendor}
					onchange={() => onVendorChange(cap.vendor as WritebackVendor)}
				/>
				{cap.vendor}
				{#if !cap.available}<span class="reason"> ({cap.reason})</span>{/if}
			</label>
		{/each}
	</div>
{:else}
	<p>Loading capabilities...</p>
{/if}

{#if loading}
	<p>Working...</p>
{:else if plan}
	<h3>Plan: {plan.playlist_name} -&gt; {plan.vendor}</h3>
	<p>Target playlist {plan.target_exists ? 'already exists' : 'does not exist yet'}.</p>
	{#if plan.is_noop}
		<p>No changes -- target already matches.</p>
	{:else}
		<div class="diff-columns">
			<div>
				<h4>Add ({plan.added.length})</h4>
				<ul>{#each plan.added as sid}<li>{sid}</li>{/each}</ul>
			</div>
			<div>
				<h4>Remove ({plan.removed.length})</h4>
				<ul>{#each plan.removed as sid}<li>{sid}</li>{/each}</ul>
			</div>
		</div>
	{/if}
	{#if plan.unresolved.length > 0}
		<p class="warn">
			{plan.unresolved.length} track(s) have no {plan.vendor} mapping and will not be written:
			{plan.unresolved.join(', ')}
		</p>
	{/if}
	{#if plan.target_exists}
		<label>
			<input type="checkbox" bind:checked={forceAdopt} />
			I reviewed the plan above -- write into the existing {plan.vendor} playlist
		</label>
	{/if}
	<button
		onclick={onApply}
		disabled={plan.is_noop || (plan.target_exists && !forceAdopt) || plan.unresolved.length > 0}
	>
		Apply to {plan.vendor}
	</button>
{/if}

{#if applyResult}
	<h3>Result</h3>
	{#if applyResult.applied}
		<p>Applied: +{applyResult.added.length} / -{applyResult.removed.length}</p>
	{:else}
		<p class="warn">Not applied: {applyResult.error}</p>
	{/if}
{/if}

<style>
	.vendor-picker {
		display: flex;
		gap: 1rem;
		margin-bottom: 1rem;
	}
	.reason {
		color: var(--muted);
	}
	.diff-columns {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 1rem;
	}
	.warn {
		color: var(--error, #c0392b);
	}
</style>
