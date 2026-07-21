<script lang="ts">
	// Build unit: mixer (COMPONENT-MAP 1.4). Stub - owner replaces the body.
	// 4 channel strips in screen order 3 1 2 4: TRIM + HIGH/MID/LOW knobs,
	// channel fader, crossfader + assign matrix (all real via audio engine).
	// Inert: headphone CUE buttons, MIX/LEVEL knobs (no headphone bus).
</script>

<section class="rb-mixer rb-panel">
	<span>Mixer stub - build unit: mixer</span>
</section>

<style>
	.rb-mixer {
		grid-area: mixer;
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--rb-text-dim);
		min-height: 0;
	}
</style>
