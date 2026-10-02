<script lang="ts">
	/** App-shell topbar: FB-17 pins-visible checkbox (library and admin routes). */
	import { onMount } from 'svelte';
	import { onPinsVisibleChanged, readPinsVisible, writePinsVisible } from '$lib/rb/feedback-pin-visibility';

	let pinsVisible = $state(false);

	function syncPinsVisible(): void {
		pinsVisible = readPinsVisible(window.localStorage);
	}

	onMount(() => {
		syncPinsVisible();
		const off = onPinsVisibleChanged(syncPinsVisible);
		return () => off();
	});

	function togglePinsVisible(): void {
		pinsVisible = !pinsVisible;
		writePinsVisible(window.localStorage, pinsVisible);
	}
</script>

<label class="fb-shell-pins-toggle" title="Show or hide feedback comment pins on every page">
	<input type="checkbox" checked={pinsVisible} onchange={togglePinsVisible} />
	Show feedback comment pins
</label>

<style>
	.fb-shell-pins-toggle {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		font-size: 12px;
		color: var(--muted);
		cursor: pointer;
		white-space: nowrap;
	}
	.fb-shell-pins-toggle input {
		margin: 0;
	}
</style>
