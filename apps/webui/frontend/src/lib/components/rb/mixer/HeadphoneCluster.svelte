<script lang="ts">
	/**
	 * Headphone MIX + LEVEL knobs with headphone icon (SCREENSHOT-SPEC 4).
	 * Real CUE/MASTER monitor mix, level, and browser-selected output device.
	 */
	import { knobId } from '$lib/rb/knob-control.svelte';
	import Knob from './Knob.svelte';
	import type { HeadphoneState } from '$lib/rb/types';

	interface Props {
		state: HeadphoneState;
		onmix: (value: number) => void;
		onlevel: (value: number) => void;
		onrefresh: () => void;
		onacquire: () => void;
		onselect: (deviceId: string) => void;
	}

	let { state, onmix, onlevel, onrefresh, onacquire, onselect }: Props = $props();
</script>

<div class="hp" data-performance-control="headphones">
	<svg class="hp-icon" width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
		<!-- headphone band + ear cups -->
		<path
			d="M2 8 V6 a4 4 0 0 1 8 0 v2"
			fill="none"
			stroke="currentColor"
			stroke-width="1.2"
		/>
		<rect x="1" y="7" width="2.4" height="3.4" rx="0.8" fill="currentColor" />
		<rect x="8.6" y="7" width="2.4" height="3.4" rx="0.8" fill="currentColor" />
	</svg>
	<Knob knobId={knobId('hp', 'hp-mix')} label="MIX" value={state.mix} onchange={onmix} />
	<Knob knobId={knobId('hp', 'hp-level')} label="LEVEL" value={state.level} onchange={onlevel} />
	<button
		type="button"
		class="hp-btn"
		title="Grant browser access to a second audio output for headphones"
		onclick={onacquire}>+ OUT</button
	>
	<button
		type="button"
		class="hp-btn"
		title="Rescan available headphone output devices"
		onclick={onrefresh}>↻</button
	>
	<select
		aria-label="headphone output device"
		title="Headphone output device"
		value={state.selected_output_device_id ?? ''}
		disabled={!state.supported}
		onchange={(event) => onselect(event.currentTarget.value)}
	>
		<option value="" disabled>HP out</option>
		{#each state.outputs as output (output.id)}
			<option value={output.id}>{output.label || output.id}</option>
		{/each}
	</select>
	{#if state.error !== null}<span class="hp-error">{state.error}</span>{/if}
</div>

<style>
	.hp {
		display: flex;
		align-items: center;
		gap: 4px;
	}
	.hp-icon {
		color: var(--rb-text-dim);
		flex: 0 0 auto;
	}
	.hp-btn {
		font: inherit;
		font-size: 7px;
		letter-spacing: 0.04em;
		padding: 1px 4px;
		line-height: 1.2;
		max-width: 42px;
		background: var(--rb-panel-raised, #1a1e25);
		border: 1px solid var(--rb-border, #23282f);
		border-radius: 2px;
		color: var(--rb-text-dim, #7a8088);
		cursor: pointer;
	}
	.hp-btn:hover {
		color: var(--rb-text, #c8cdd2);
		border-color: var(--rb-accent, #2f6fd6);
	}
	select {
		font: inherit;
		font-size: 7px;
		max-width: 72px;
		padding: 0 2px;
		background: var(--rb-panel-raised, #1a1e25);
		border: 1px solid var(--rb-border, #23282f);
		color: var(--rb-text-dim, #7a8088);
	}
	.hp-error {
		color: var(--rb-danger, #ff6b6b);
		font-size: 7px;
		max-width: 100px;
	}
</style>
