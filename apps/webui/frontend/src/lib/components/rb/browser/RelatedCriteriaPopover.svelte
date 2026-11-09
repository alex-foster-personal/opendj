<script lang="ts">
	// LIBUX-10046: the Related tab's criteria cog (rekordbox Related Tracks
	// settings). Props only, so this popover never imports prefs.svelte; the
	// parent saves each change and refetches.
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import {
		isValidRelatedBpmPercent,
		RELATED_BPM_PERCENT_MAX,
		RELATED_BPM_PERCENT_MIN,
		RELATED_CRITERIA_FLAGS,
		type RelatedCriteriaPrefs
	} from '$lib/rb/related-criteria-prefs';

	let {
		criteria,
		onchange,
		onreset
	}: {
		criteria: RelatedCriteriaPrefs;
		/** One changed field per call. */
		onchange: (patch: Partial<RelatedCriteriaPrefs>) => void;
		/** Restore the #9753 defaults. */
		onreset: () => void;
	} = $props();

	let open = $state(false);
	let triggerEl = $state<HTMLButtonElement | null>(null);
	let popoverEl = $state<HTMLDivElement | null>(null);

	function _close(): void {
		open = false;
		triggerEl?.focus();
	}

	function _toggle(): void {
		if (open) _close();
		else open = true;
	}

	function _onBpmChange(event: Event): void {
		const input = event.currentTarget as HTMLInputElement;
		const value = Number(input.value);
		if (isValidRelatedBpmPercent(value)) onchange({ bpm_percent: value });
		else input.value = String(criteria.bpm_percent);
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

<span class="related-criteria-pop">
	<button
		type="button"
		class="related-criteria-cog"
		class:lit={open}
		bind:this={triggerEl}
		aria-label="Related tracks criteria"
		aria-expanded={open}
		aria-haspopup="dialog"
		title="Related tracks criteria"
		data-testid="related-criteria-cog"
		onclick={_toggle}
	>
		<svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true">
			<path
				d="M8 5.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zm6 3.3V7.2l-1.7-.4-.4-1 .9-1.5-1.1-1.1-1.5.9-1-.4L8.8 2H7.2l-.4 1.7-1 .4-1.5-.9-1.1 1.1.9 1.5-.4 1L2 7.2v1.6l1.7.4.4 1-.9 1.5 1.1 1.1 1.5-.9 1 .4.4 1.7h1.6l.4-1.7 1-.4 1.5.9 1.1-1.1-.9-1.5.4-1z"
				fill="currentColor"
			/>
		</svg>
	</button>
	{#if open}
		<div
			class="related-criteria-float"
			role="dialog"
			aria-label="Related tracks criteria"
			bind:this={popoverEl}
			use:triggerFloatingAction={{ getTrigger: () => triggerEl, preferred: 'below', gap: 4 }}
		>
			<label class="row">
				<span>BPM range (%)</span>
				<input
					type="number"
					min={RELATED_BPM_PERCENT_MIN}
					max={RELATED_BPM_PERCENT_MAX}
					step="1"
					value={criteria.bpm_percent}
					data-testid="related-bpm-percent"
					onchange={_onBpmChange}
				/>
			</label>
			{#each RELATED_CRITERIA_FLAGS as flag (flag.name)}
				<label class="row">
					<input
						type="checkbox"
						checked={criteria[flag.name]}
						data-testid={`related-crit-${flag.name}`}
						onchange={(e) =>
							onchange({ [flag.name]: (e.currentTarget as HTMLInputElement).checked })}
					/>
					<span>{flag.label}</span>
				</label>
			{/each}
			<button
				type="button"
				class="reset"
				data-testid="related-criteria-reset"
				onclick={() => onreset()}
			>
				Reset to defaults
			</button>
		</div>
	{/if}
</span>

<style>
	.related-criteria-pop {
		position: relative;
		display: inline-flex;
		align-items: center;
	}

	.related-criteria-cog {
		background: none;
		border: 0;
		padding: 0 2px;
		color: inherit;
		cursor: pointer;
		opacity: 0.7;
	}

	.related-criteria-cog.lit,
	.related-criteria-cog:hover {
		opacity: 1;
	}

	.related-criteria-float {
		z-index: 40;
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 8px;
		min-width: 190px;
		background: var(--rb-panel-bg, #1c1c1c);
		border: 1px solid var(--rb-border, #333);
		border-radius: 4px;
		font-size: 11px;
	}

	.row {
		display: flex;
		align-items: center;
		gap: 6px;
	}

	.row input[type='number'] {
		width: 48px;
		margin-left: auto;
	}

	.reset {
		margin-top: 4px;
		align-self: flex-start;
	}
</style>
