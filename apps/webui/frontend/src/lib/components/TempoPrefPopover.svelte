<script lang="ts">
	// PREF-01: right-click-on-BPM editor for a track's preferred ("regular")
	// tempo plus its playable min/max range. Fetches the track fresh on open
	// (etag for CAS + current values) rather than trusting a possibly-stale
	// row prop - this is an infrequent action, not a hot path.
	import { getTrack, patchTrack, ConflictError } from '$lib/api';
	import { pushToast } from '$lib/stores.svelte';

	let {
		stableId,
		x,
		y,
		onclose
	}: { stableId: string; x: number; y: number; onclose: () => void } = $props();

	let loading = $state(true);
	let loadError = $state<string | null>(null);
	let etag = $state('');
	let regular = $state('');
	let min = $state('');
	let max = $state('');
	let saving = $state(false);
	let saveError = $state<string | null>(null);
	let panelEl: HTMLDivElement | undefined = $state();

	function _fmt(v: number | null | undefined): string {
		return v === null || v === undefined ? '' : String(v);
	}

	async function _load(): Promise<void> {
		loading = true;
		loadError = null;
		try {
			const { track, etag: fresh } = await getTrack(stableId);
			etag = fresh;
			regular = _fmt(track.tempo_pref?.regular);
			min = _fmt(track.tempo_pref?.min);
			max = _fmt(track.tempo_pref?.max);
		} catch (exc) {
			loadError = `Failed to load track: ${String(exc)}`;
		} finally {
			loading = false;
		}
	}
	void _load();

	function _parseOrNull(raw: string): number | null {
		const trimmed = raw.trim();
		if (trimmed === '') return null;
		const n = Number(trimmed);
		return Number.isFinite(n) ? n : null;
	}

	const parsedMin = $derived(_parseOrNull(min));
	const parsedMax = $derived(_parseOrNull(max));
	const rangeInvalid = $derived(
		parsedMin !== null && parsedMax !== null && parsedMin >= parsedMax
	);

	async function _save(): Promise<void> {
		if (rangeInvalid) return;
		saving = true;
		saveError = null;
		try {
			const parsedRegular = _parseOrNull(regular);
			// All three unset means "no preference" - the API models that as
			// tempo_pref: null, not an object of nulls (a non-null object with
			// no values still shows as a set preference + provenance on reread).
			const tempoPref =
				parsedRegular === null && parsedMin === null && parsedMax === null
					? null
					: { regular: parsedRegular, min: parsedMin, max: parsedMax };
			const { etag: fresh } = await patchTrack(stableId, etag, {
				tempo_pref: tempoPref
			});
			etag = fresh;
			onclose();
		} catch (exc) {
			if (exc instanceof ConflictError) {
				saveError = 'This track changed elsewhere - reloading current values.';
				etag = exc.etag;
				regular = _fmt(exc.current.tempo_pref?.regular);
				min = _fmt(exc.current.tempo_pref?.min);
				max = _fmt(exc.current.tempo_pref?.max);
				return;
			}
			saveError = `Save failed: ${String(exc)}`;
			pushToast(saveError, 'error');
		} finally {
			saving = false;
		}
	}

	function _clear(): void {
		regular = '';
		min = '';
		max = '';
		void _save();
	}

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
	style={`left:${x}px;top:${y}px`}
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
			<button type="button" onclick={_clear} disabled={saving}>Clear</button>
			<button type="button" onclick={onclose} disabled={saving}>Cancel</button>
			<button type="button" onclick={_save} disabled={saving || rangeInvalid}>Save</button>
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
