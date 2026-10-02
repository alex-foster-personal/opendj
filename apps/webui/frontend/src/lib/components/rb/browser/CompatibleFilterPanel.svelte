<script lang="ts">
	import { patchCompatibleFilter, uiPrefs } from '$lib/rb/prefs.svelte';

	// Loaded on first hover by CompatibleFilterPopover, so the range buttons stay
	// out of the /performance route's eager bundle.
	let { onEnter, onLeave }: { onEnter: () => void; onLeave: () => void } = $props();

	// A range change commits only after the disk write lands (PR #4014, Sol P1);
	// a rejection is shown here instead of leaving a range the disk never took.
	let saveError = $state<string | null>(null);

	function _patch(patch: Parameters<typeof patchCompatibleFilter>[0]): void {
		saveError = null;
		patchCompatibleFilter(patch).catch((err: unknown) => {
			const detail = err instanceof Error ? err.message : String(err);
			saveError = `Could not save compatible filter: ${detail}`;
		});
	}

	function _setCamelot(steps: 0 | 1 | 2): void {
		_patch({ camelot_steps: steps });
	}

	function _setBpmWindow(n: number): void {
		_patch({ bpm_window_bpm: n, bpm_enabled: true });
	}

	function _setBpmOff(): void {
		_patch({ bpm_enabled: false });
	}

	function _setDirection(d: 'both' | 'above' | 'below' | 'same'): void {
		_patch({ bpm_direction: d });
	}
</script>

<div
	class="compat-filter-panel"
	role="group"
	aria-label="Compatible filter options"
	onmouseenter={onEnter}
	onmouseleave={onLeave}
>
	<p class="compat-filter-heading">Camelot steps</p>
	<div class="compat-filter-row">
		<button type="button" onclick={() => _setCamelot(0)}>same key</button>
		<button type="button" onclick={() => _setCamelot(1)}>±1</button>
		<button type="button" onclick={() => _setCamelot(2)}>±2</button>
	</div>
	<p class="compat-filter-heading">BPM window</p>
	<div class="compat-filter-row">
		<button type="button" onclick={() => _setBpmWindow(10)}>±10</button>
		<button type="button" onclick={() => _setBpmWindow(20)}>±20</button>
		<button type="button" onclick={_setBpmOff}>off</button>
	</div>
	<p class="compat-filter-heading">BPM direction</p>
	<div class="compat-filter-row">
		<button type="button" onclick={() => _setDirection('both')}>±</button>
		<button type="button" onclick={() => _setDirection('above')}>above</button>
		<button type="button" onclick={() => _setDirection('below')}>below</button>
		<button type="button" onclick={() => _setDirection('same')}>same</button>
	</div>
	<p class="compat-filter-summary" title="Current compatible filter settings">
		Camelot ±{uiPrefs.compatible_filter.camelot_steps},
		{uiPrefs.compatible_filter.bpm_enabled
			? `BPM ±${uiPrefs.compatible_filter.bpm_window_bpm} (${uiPrefs.compatible_filter.bpm_direction})`
			: 'BPM off'}
	</p>
	{#if saveError}
		<p class="compat-filter-error" role="alert" title={saveError}>{saveError}</p>
	{/if}
</div>

<style>
	.compat-filter-panel {
		position: absolute;
		top: 100%;
		left: 0;
		z-index: 50;
		min-width: 12rem;
		padding: 0.5rem;
		background: var(--rb-panel-bg, #1a1a1a);
		border: 1px solid var(--rb-border, #444);
		border-radius: 6px;
	}
	.compat-filter-heading {
		margin: 0.25rem 0 0.15rem;
		font-size: 0.7rem;
		opacity: 0.8;
	}
	.compat-filter-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.25rem;
	}
	.compat-filter-row button {
		font-size: 0.7rem;
		padding: 0.15rem 0.35rem;
	}
	.compat-filter-error {
		margin: 0.35rem 0 0;
		font-size: 0.65rem;
		color: var(--rb-danger, #f66);
	}
	.compat-filter-summary {
		margin: 0.35rem 0 0;
		font-size: 0.65rem;
	}
</style>
