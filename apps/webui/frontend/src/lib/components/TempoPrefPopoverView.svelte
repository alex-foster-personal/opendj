<script lang="ts">
	// Props-only half of TempoPrefPopover (PREF-01): the container fetches the
	// track, owns the etag and the save, and places the panel; this file draws
	// the form. The three tempo fields are bindable NUMBERS (see the container
	// for why), and the panel ref is bindable so the container can measure it.
	let {
		panelEl = $bindable(),
		position,
		loading,
		loadError,
		regular = $bindable(),
		min = $bindable(),
		max = $bindable(),
		rangeInvalid,
		saving,
		saveError,
		onSave,
		onClear,
		onclose
	}: {
		panelEl?: HTMLDivElement | undefined;
		position: { x: number; y: number };
		loading: boolean;
		loadError: string | null;
		regular: number | null;
		min: number | null;
		max: number | null;
		rangeInvalid: boolean;
		saving: boolean;
		saveError: string | null;
		onSave: () => void;
		onClear: () => void;
		onclose: () => void;
	} = $props();

	function _keydown(event: KeyboardEvent): void {
		if (event.key === 'Escape') {
			event.preventDefault();
			onclose();
		}
	}

	function _clickOutside(event: MouseEvent): void {
		if (panelEl && !panelEl.contains(event.target as Node)) onclose();
	}
</script>

<svelte:window onkeydown={_keydown} onmousedown={_clickOutside} />

<div
	bind:this={panelEl}
	class="tempo-pref-popover"
	style={`left:${position.x}px;top:${position.y}px`}
	role="dialog"
	aria-label="Set preferred tempo and playable range"
>
	{#if loading}
		<span class="tp-status">loading...</span>
	{:else if loadError}
		<span class="tp-status tp-error">{loadError}</span>
	{:else}
		<label class="tp-regular">
			<span>Regular (preferred) tempo</span>
			<input type="number" step="0.1" bind:value={regular} placeholder="unset" />
		</label>
		<div class="tp-range">
			<label class="tp-minmax">
				<span>Min</span>
				<input type="number" step="0.1" bind:value={min} placeholder="unset" />
			</label>
			<label class="tp-minmax">
				<span>Max</span>
				<input type="number" step="0.1" bind:value={max} placeholder="unset" />
			</label>
		</div>
		{#if rangeInvalid}
			<span class="tp-status tp-error">min must be less than max</span>
		{/if}
		{#if saveError}
			<span class="tp-status tp-error">{saveError}</span>
		{/if}
		<div class="tp-actions">
			<button type="button" onclick={onClear} disabled={saving}>Clear</button>
			<button type="button" onclick={onclose} disabled={saving}>Cancel</button>
			<button type="button" onclick={onSave} disabled={saving || rangeInvalid}>Save</button>
		</div>
	{/if}
</div>

<style>
	.tempo-pref-popover {
		position: fixed;
		z-index: 50;
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 10px;
		background: #1a212c;
		border: 1px solid #333;
		border-radius: 6px;
		box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
		min-width: 220px;
	}
	.tp-regular {
		display: flex;
		flex-direction: column;
		gap: 2px;
		font-size: 13px;
	}
	.tp-regular input {
		font-size: 15px;
		padding: 4px;
	}
	.tp-range {
		display: flex;
		gap: 8px;
	}
	.tp-minmax {
		display: flex;
		flex-direction: column;
		gap: 2px;
		font-size: 11px;
		color: #999;
		flex: 1;
	}
	.tp-minmax input {
		font-size: 12px;
		padding: 2px;
	}
	.tp-status {
		font-size: 12px;
	}
	.tp-error {
		color: #e05555;
	}
	.tp-actions {
		display: flex;
		justify-content: flex-end;
		gap: 6px;
	}
</style>
