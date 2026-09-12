<script lang="ts">
	/**
	 * Headphone MIX + LEVEL knobs with headphone icon (SCREENSHOT-SPEC 4).
	 * Real CUE/MASTER monitor mix, level, and browser-selected output device.
	 */
	import { knobId } from '$lib/rb/knob-control.svelte';
	import Knob from './Knob.svelte';
	import type { HeadphoneOutputMode, HeadphoneState } from '$lib/rb/mixer-types';

	interface Props {
		state: HeadphoneState;
		onmix: (value: number) => void;
		onlevel: (value: number) => void;
		onrefresh: () => void;
		onacquire: () => void;
		onselect: (deviceId: string) => void;
		onmode: (mode: HeadphoneOutputMode) => void;
	}

	let { state, onmix, onlevel, onrefresh, onacquire, onselect, onmode }: Props = $props();

	const modeLabel = $derived(
		state.output_mode === 'practice'
			? 'practice'
			: state.output_mode === 'two_outputs'
				? 'two outputs'
				: 'split cable'
	);

	function toggleSplit(): void {
		if (state.output_mode === 'split_cable') {
			onmode(state.selected_output_device_id !== null ? 'two_outputs' : 'practice');
		} else {
			onmode('split_cable');
		}
	}
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
	<span
		class="hp-mode"
		data-output-mode={state.output_mode}
		title={
			state.output_mode === 'practice'
				? 'Practice: MIX blends cue into the main output'
				: state.output_mode === 'two_outputs'
					? 'Two outputs: MIX feeds the monitor only'
					: 'Split cable: mono master on LEFT, mono cue on RIGHT'
		}>{modeLabel}</span
	>
	<button
		type="button"
		class="hp-btn"
		aria-pressed={state.output_mode === 'split_cable'}
		aria-label="Split cable output mode"
		title="Room feed is mono on LEFT, cue is mono on RIGHT. Use a DJ splitter cable, not a Y cable."
		onclick={toggleSplit}>SPLIT</button
	>
	<button
		type="button"
		class="hp-btn"
		aria-label="ADD OUTPUT"
		title="Grant browser access to a second audio output for headphones"
		onclick={onacquire}>+ OUT</button
	>
	<button
		type="button"
		class="hp-btn"
		title="Rescan available headphone output devices"
		aria-label="Rescan available headphone output devices"
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
	{#if state.output_mode === 'split_cable'}
		<span class="hp-split-warn" title="Split-cable mode requires a true DJ splitter cable, not a Y cable">
			Room feed is mono. Use a DJ splitter cable, not a Y cable.
		</span>
	{/if}
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
		color: var(--rb-text-dim, #838990);
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
		color: var(--rb-text-dim, #838990);
	}
	.hp-mode {
		font-size: 7px;
		letter-spacing: 0.04em;
		color: var(--rb-text-dim, #838990);
		white-space: nowrap;
	}
	.hp-split-warn {
		font-size: 7px;
		color: var(--rb-warn, #e6a23c);
		max-width: 120px;
		line-height: 1.2;
	}
	.hp-error {
		color: var(--rb-danger, #ff6b6b);
		font-size: 7px;
		max-width: 100px;
	}
</style>
