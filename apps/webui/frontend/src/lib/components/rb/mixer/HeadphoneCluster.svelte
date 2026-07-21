<script lang="ts">
	/**
	 * Headphone MIX + LEVEL knobs with headphone icon (SCREENSHOT-SPEC 4).
	 * Entirely inert at v1: single output device, no headphone bus
	 * (COMPONENT-MAP 1.4 "mixer aux").
	 */
	import Knob from './Knob.svelte';

	const INERT_TITLE = 'not implemented - see PARITY-TODO';
</script>

<div class="hp" title={INERT_TITLE}>
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
	<Knob label="MIX" value={0.5} inert={true} />
	<Knob label="LEVEL" value={0.5} inert={true} />
</div>

<style>
	.hp {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.hp-icon {
		color: var(--rb-text-dim);
		flex: 0 0 auto;
	}
</style>
