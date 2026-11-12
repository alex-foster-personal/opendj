<script lang="ts">
	// TOPVIEW-01..03: what the /performance wavestack grid area shows, from
	// the top bar's view icons (uiPrefs.perf_view). Mount behaviour comes from
	// view-registry.ts so agents extend one table instead of forking this file.
	import { perfViewWavestackMount } from '$lib/rb/view-registry';
	import type { PerfView } from '$lib/rb/topbar-view-prefs';
	import WaveformStack from './WaveformStack.svelte';

	let { view, lessLayout }: { view: PerfView; lessLayout: boolean } = $props();

	const mount = $derived(perfViewWavestackMount(view));

	const loadPhaseScope = () => import('./wave/PhaseScopeView.svelte');
	const loadPhaseMeter = () => import('./wave/PhaseMeterView.svelte');
</script>

{#snippet failed(err: unknown)}
	<!-- The error stands where the view would: in the wavestack slot, and in
	     the phase-view region technically-working mode hides and reveals with
	     the top edge (Sol P1 4349621975, Codex P2 4349412308). -->
	<p class="view-load-error rb-phase-view" role="alert">
		This view failed to load: {err instanceof Error ? err.message : String(err)}. Reload the page to
		retry.
	</p>
{/snippet}

{#if mount === 'waveform'}
	<WaveformStack />
{:else if mount === 'phase-scope'}
	{#await loadPhaseScope() then { default: PhaseScopeView }}
		<PhaseScopeView {lessLayout} />
	{:catch err}
		{@render failed(err)}
	{/await}
{:else if mount === 'phase-meter'}
	{#await loadPhaseMeter() then { default: PhaseMeterView }}
		<PhaseMeterView {lessLayout} />
	{:catch err}
		{@render failed(err)}
	{/await}
{/if}

<style>
	.view-load-error {
		grid-area: wavestack;
		margin: 0;
		padding: 8px 12px;
		color: var(--rb-red);
		font-size: 12px;
	}
</style>
