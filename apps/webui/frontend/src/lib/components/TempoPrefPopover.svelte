<script lang="ts">
	// PREF-01: right-click-on-BPM editor for a track's preferred ("regular")
	// tempo plus its playable min/max range. Fetches the track fresh on open
	// (etag for CAS + current values) rather than trusting a possibly-stale
	// row prop - this is an infrequent action, not a hot path.
	import { tick } from 'svelte';
	import { getTrack, patchTrack, ConflictError } from '$lib/api';
	import { clampToViewport } from '$lib/ui/clamp-to-viewport';
	import { pushToast } from '$lib/stores.svelte';
	import {
		tempoPrefFieldValue,
		tempoPrefPatchValue,
		tempoPrefRangeInvalid
	} from '$lib/rb/tempo-pref-fields';
	import TempoPrefPopoverView from './TempoPrefPopoverView.svelte';

	let { stableId, x, y, onclose }: { stableId: string; x: number; y: number; onclose: () => void } =
		$props();

	let loading = $state(true);
	let loadError = $state<string | null>(null);
	let etag = $state('');
	// NUMBERS, not strings. `bind:value` on `<input type="number">` writes a
	// number (or undefined when the field is emptied, which the
	// tempo-pref-fields helpers treat as unset), so holding these as strings
	// meant every keystroke replaced the string with a number and the string
	// parser below then threw `raw.trim is not a function` out of a $derived.
	// That killed the range-invalid message and the Save handler, silently:
	// the popover stayed open with no error shown. svelte-check cannot see
	// this, because the coercion happens inside the binding.
	let regular = $state<number | null | undefined>(null);
	let min = $state<number | null | undefined>(null);
	let max = $state<number | null | undefined>(null);
	let saving = $state(false);
	let saveError = $state<string | null>(null);
	let panelEl: HTMLDivElement | undefined = $state();
	let position = $state({ x: 0, y: 0 });

	async function placePanel(): Promise<void> {
		await tick();
		if (panelEl === undefined) return;
		const rect = panelEl.getBoundingClientRect();
		position = clampToViewport(
			x,
			y,
			{ width: rect.width, height: rect.height },
			{ width: window.innerWidth, height: window.innerHeight }
		);
	}

	// Re-clamp once loading settles too: the panel is measured while it only
	// says "loading...", and the loaded form is wider and taller, so an anchor
	// near the window's right or bottom edge pushed Save off screen.
	$effect(() => {
		void x;
		void y;
		void loading;
		void loadError;
		void placePanel();
	});

	async function _load(): Promise<void> {
		loading = true;
		loadError = null;
		try {
			const { track, etag: fresh } = await getTrack(stableId);
			etag = fresh;
			regular = tempoPrefFieldValue(track.tempo_pref?.regular);
			min = tempoPrefFieldValue(track.tempo_pref?.min);
			max = tempoPrefFieldValue(track.tempo_pref?.max);
		} catch (exc) {
			loadError = `Failed to load track: ${String(exc)}`;
		} finally {
			loading = false;
		}
	}
	void _load();

	const rangeInvalid = $derived(tempoPrefRangeInvalid(min, max));

	async function _save(): Promise<void> {
		if (rangeInvalid) return;
		saving = true;
		saveError = null;
		try {
			// All three unset means "no preference" - the API models that as
			// tempo_pref: null, not an object of nulls (a non-null object with
			// no values still shows as a set preference + provenance on reread).
			const tempoPref = tempoPrefPatchValue(regular, min, max);
			const { etag: fresh } = await patchTrack(stableId, etag, {
				tempo_pref: tempoPref
			});
			etag = fresh;
			onclose();
		} catch (exc) {
			if (exc instanceof ConflictError) {
				saveError = 'This track changed elsewhere - reloading current values.';
				etag = exc.etag;
				regular = tempoPrefFieldValue(exc.current.tempo_pref?.regular);
				min = tempoPrefFieldValue(exc.current.tempo_pref?.min);
				max = tempoPrefFieldValue(exc.current.tempo_pref?.max);
				return;
			}
			const message = `Save failed: ${String(exc)}`;
			saveError = message;
			pushToast(message, 'error');
		} finally {
			saving = false;
		}
	}

	function _clear(): void {
		regular = null;
		min = null;
		max = null;
		void _save();
	}
</script>

<TempoPrefPopoverView
	bind:panelEl
	{position}
	{loading}
	{loadError}
	bind:regular
	bind:min
	bind:max
	{rangeInvalid}
	{saving}
	{saveError}
	onSave={_save}
	onClear={_clear}
	{onclose}
/>
