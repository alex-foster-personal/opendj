<script lang="ts">
	// Issue #9738: the BPM column funnel, min and max BPM (inclusive), applied to the
	// current pane and cleared with one click (rekordbox Track Filter BPM range, mimicked).
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import {
		bpmFilterActive,
		EMPTY_BPM_FILTER,
		parseBpmRangeInputs,
		type BpmColumnFilter
	} from './track-table-column-filters';

	let {
		filter,
		onpatch
	}: {
		filter: BpmColumnFilter;
		onpatch: (next: BpmColumnFilter) => void;
	} = $props();

	let open = $state(false);
	let minText = $state('');
	let maxText = $state('');
	let triggerEl = $state<HTMLButtonElement | null>(null);
	let popoverEl = $state<HTMLDivElement | null>(null);

	const active = $derived(bpmFilterActive(filter));
	const parsed = $derived(parseBpmRangeInputs(minText, maxText));
	const triggerTitle = $derived(
		active
			? `BPM filter: ${filter.min ?? '...'} - ${filter.max ?? '...'} (click to change)`
			: 'Filter rows by BPM range'
	);

	function _close(): void {
		open = false;
		triggerEl?.focus();
	}

	function _toggle(): void {
		if (!open) {
			minText = filter.min === null ? '' : String(filter.min);
			maxText = filter.max === null ? '' : String(filter.max);
		}
		open = !open;
	}

	function _apply(): void {
		if (!parsed.ok) return;
		onpatch(parsed.filter);
		_close();
	}

	function _clear(): void {
		onpatch(EMPTY_BPM_FILTER);
		_close();
	}

	function onInputKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Enter') return;
		event.preventDefault();
		_apply();
	}

	function onWindowKeydown(event: KeyboardEvent): void {
		if (!open || event.key !== 'Escape') return;
		event.preventDefault();
		_close();
	}

	function onWindowPointerDown(event: PointerEvent): void {
		if (!open) return;
		const target = event.target;
		if (!(target instanceof Node)) return;
		if (triggerEl?.contains(target)) return;
		if (popoverEl?.contains(target)) return;
		_close();
	}
</script>

<svelte:window onkeydown={onWindowKeydown} onpointerdown={onWindowPointerDown} />

<span class="bpm-funnel-pop">
	<button
		type="button"
		class="bpm-funnel-trigger"
		class:lit={active || open}
		bind:this={triggerEl}
		aria-expanded={open}
		aria-haspopup="dialog"
		aria-label="BPM range filter"
		title={triggerTitle}
		data-testid="bpm-funnel-trigger"
		onclick={_toggle}
	>
		<svg viewBox="0 0 16 16" width="9" height="9" aria-hidden="true">
			<path d="M2 3h12l-4.5 5v5l-3-1.5V8z" fill="currentColor" />
		</svg>
	</button>
	{#if open}
		<div
			class="bpm-funnel-float"
			role="dialog"
			aria-label="BPM column filter"
			bind:this={popoverEl}
			use:triggerFloatingAction={{ getTrigger: () => triggerEl, preferred: 'below', gap: 4 }}
		>
			<div class="range">
				<label>
					Min
					<input
						type="number"
						min="0"
						step="0.1"
						value={minText}
						oninput={(e) => (minText = e.currentTarget.value)}
						onkeydown={onInputKeydown}
						data-testid="bpm-funnel-min"
					/>
				</label>
				<label>
					Max
					<input
						type="number"
						min="0"
						step="0.1"
						value={maxText}
						oninput={(e) => (maxText = e.currentTarget.value)}
						onkeydown={onInputKeydown}
						data-testid="bpm-funnel-max"
					/>
				</label>
			</div>
			{#if !parsed.ok}
				<div class="reason" role="alert">{parsed.reason}</div>
			{/if}
			<div class="actions">
				<button type="button" class="clear" disabled={!active} onclick={_clear}>Clear</button>
				<button type="button" class="apply" disabled={!parsed.ok} onclick={_apply}>Apply</button>
			</div>
		</div>
	{/if}
</span>

<style>
	.bpm-funnel-pop {
		position: relative;
		display: inline-flex;
		align-items: center;
		margin-left: 2px;
	}

	.bpm-funnel-trigger {
		padding: 0 1px;
		border: none;
		background: transparent;
		color: var(--rb-text-dim, #888);
		cursor: pointer;
		line-height: 0;
	}

	.bpm-funnel-trigger.lit {
		color: var(--rb-accent, #4fb2ff);
	}

	.bpm-funnel-float {
		z-index: 40;
		padding: 6px;
		background: var(--rb-panel-bg, #1a1a1a);
		border: 1px solid var(--rb-border, #444);
		border-radius: 4px;
		min-width: 140px;
		font-size: 10px;
	}

	.range {
		display: flex;
		gap: 6px;
	}

	.range label {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.range input {
		width: 56px;
		font: inherit;
	}

	.reason {
		margin-top: 4px;
		color: var(--rb-danger, #ff6b6b);
	}

	.actions {
		margin-top: 6px;
		display: flex;
		justify-content: flex-end;
		gap: 4px;
	}

	.actions button {
		font: inherit;
		padding: 2px 6px;
		cursor: pointer;
	}

	.actions button:disabled {
		opacity: 0.4;
		cursor: default;
	}
</style>
