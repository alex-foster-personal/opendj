<script lang="ts">
	import type { Snippet } from 'svelte';
	import { patchCompatibleFilter, uiPrefs } from '$lib/rb/prefs.svelte';

	let { children }: { children: Snippet } = $props();
	let open = $state(false);
	let closeTimer: ReturnType<typeof setTimeout> | undefined;

	function _clearCloseTimer(): void {
		if (closeTimer !== undefined) {
			clearTimeout(closeTimer);
			closeTimer = undefined;
		}
	}

	function _open(): void {
		_clearCloseTimer();
		open = true;
	}

	function _scheduleClose(): void {
		_clearCloseTimer();
		closeTimer = setTimeout(() => {
			open = false;
			closeTimer = undefined;
		}, 120);
	}

	function _setCamelot(steps: 0 | 1 | 2): void {
		patchCompatibleFilter({ camelot_steps: steps });
	}

	function _setBpmWindow(n: number): void {
		patchCompatibleFilter({ bpm_window_bpm: n, bpm_enabled: true });
	}

	function _setBpmOff(): void {
		patchCompatibleFilter({ bpm_enabled: false });
	}

	function _setDirection(d: 'both' | 'above' | 'below' | 'same'): void {
		patchCompatibleFilter({ bpm_direction: d });
	}
</script>

<div
	class="compat-filter-pop"
	onmouseenter={_open}
	onmouseleave={_scheduleClose}
	onfocusin={_open}
	onfocusout={_scheduleClose}
>
	{@render children()}
	{#if open}
		<div
			class="compat-filter-panel"
			role="group"
			aria-label="Compatible filter options"
			onmouseenter={_clearCloseTimer}
			onmouseleave={_scheduleClose}
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
		</div>
	{/if}
</div>

<style>
	.compat-filter-pop {
		position: relative;
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
	}
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
	.compat-filter-summary {
		margin: 0.35rem 0 0;
		font-size: 0.65rem;
	}
</style>
