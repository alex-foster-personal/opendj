<script lang="ts">
	import { onMount } from 'svelte';
	import { capabilities } from '$lib/api/capabilities.svelte';
	import { entitlements } from '$lib/api/entitlements.svelte';
	import { buildFlags } from '$lib/api/store-build.svelte';
	import { rekordboxWriteback } from '$lib/rb/rekordbox-writeback.svelte';
	import { collectInspectorSnapshot } from './entitlements-inspector';

	const snapshot = $derived(collectInspectorSnapshot());

	const capabilitiesPending = $derived(
		capabilities.flavor === 'unknown' && capabilities.error === null
	);

	const emptyEntitlements = $derived(
		entitlements.loaded && entitlements.features.length === 0
	);

	onMount(() => {
		void capabilities.probe();
		void entitlements.load();
		void buildFlags.load();
		void rekordboxWriteback.probe();
	});

	function rowTitle(row: { id: string; value: string; refusal: string | null }): string {
		const refused = row.refusal ? ` Refused: ${row.refusal}` : ' Offered.';
		return `${row.id}: ${row.value}.${refused}`;
	}
</script>

<section
	class="panel"
	id="entitlements-inspector"
	data-testid="entitlements-inspector"
	aria-label="Entitlements inspector"
>
	<h3>Entitlements inspector</h3>
	<p class="sub">
		Resolved capability, entitlement, and build-flag values for this session, plus the refusal
		strings currently driving inert controls. Read-only.
	</p>

	{#if capabilitiesPending}
		<p class="note">Daemon not identified yet</p>
	{/if}
	{#if capabilities.error}
		<p class="err" title="Capabilities probe error">{capabilities.error}</p>
	{/if}
	{#if !entitlements.loaded && !entitlements.error}
		<p class="note">Reading GET /api/v1/entitlements...</p>
	{/if}
	{#if entitlements.error}
		<p class="err" title="Entitlements load error">{entitlements.error}</p>
	{/if}
	{#if emptyEntitlements}
		<p class="note">GET /api/v1/entitlements reports no gated features (ENT-04).</p>
	{/if}
	{#if !buildFlags.loaded && !buildFlags.error}
		<p class="note">Reading GET /api/v1/flags...</p>
	{/if}
	{#if buildFlags.error}
		<p class="err" title="Build flags load error">{buildFlags.error}</p>
	{/if}
	{#if rekordboxWriteback.error}
		<p class="err" title="Writeback gate probe error">{rekordboxWriteback.error}</p>
	{/if}

	<h4 class="block-title">Resolved flags</h4>
	<div class="rows">
		{#each snapshot.rows as row (row.id)}
			<div class="row" title={rowTitle(row)}>
				<span class="label">{row.label}</span>
				<span class="value">{row.value}</span>
				{#if row.refusal}
					<span class="refusal">{row.refusal}</span>
				{/if}
			</div>
		{/each}
	</div>

	<h4 class="block-title">Active refusals</h4>
	{#if snapshot.activeRefusals.length === 0}
		<p class="note">No session-level refusals right now.</p>
	{:else}
		<div class="rows">
			{#each snapshot.activeRefusals as row (row.id)}
				<div class="row active" title={rowTitle(row)}>
					<span class="label">{row.label}</span>
					<span class="refusal">{row.refusal}</span>
				</div>
			{/each}
		</div>
	{/if}
</section>

<style>
	.panel {
		max-width: 1180px;
		margin-top: 1.5rem;
	}
	h3 {
		font-size: 1rem;
		color: var(--accent);
		margin: 0 0 0.25rem 0;
	}
	.block-title {
		font-size: 0.85rem;
		color: var(--fg);
		margin: 1rem 0 0.5rem 0;
	}
	.sub {
		color: var(--muted);
		font-size: 0.85rem;
		margin: 0 0 0.75rem 0;
		max-width: 90ch;
	}
	.note {
		color: var(--muted);
		font-size: 0.8rem;
		margin: 0.25rem 0;
	}
	.err {
		color: var(--danger);
		font-size: 0.8rem;
		margin: 0.25rem 0;
	}
	.rows {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
		gap: 0.5rem;
	}
	.row {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 0.5rem 0.7rem;
		cursor: help;
	}
	.row:hover {
		border-color: var(--accent-dim);
		background: var(--surface-hover);
	}
	.row.active {
		border-color: var(--kpi-warn, var(--accent-dim));
	}
	.label {
		font-size: 0.78rem;
		color: var(--muted);
		line-height: 1.25;
	}
	.value {
		font-size: 0.9rem;
		font-weight: 600;
		color: var(--fg);
		font-variant-numeric: tabular-nums;
	}
	.refusal {
		font-size: 0.75rem;
		color: var(--muted);
		line-height: 1.3;
	}
</style>
