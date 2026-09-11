<script lang="ts">
	/** Tiny readout for the Load-to-CH click-drag intro blend. No engine writes. */
	let {
		t,
		fader,
		scrubDeltaMs
	}: { t: number; fader: number; scrubDeltaMs: number } = $props();

	const scrubSign = $derived(scrubDeltaMs >= 0 ? '+' : '');
</script>

<div class="load-blend-hud" data-testid="load-blend-hud">
	<span>t {t.toFixed(2)}</span>
	<span>scrub {scrubSign}{Math.round(scrubDeltaMs)} ms / fader {fader.toFixed(2)}</span>
</div>

<style>
	.load-blend-hud {
		position: fixed;
		z-index: 10001;
		left: 50%;
		top: 12px;
		transform: translateX(-50%);
		display: flex;
		gap: 12px;
		padding: 4px 10px;
		background: var(--rb-panel, #14171d);
		border: 1px solid rgba(180, 188, 198, 0.42);
		border-radius: 3px;
		color: var(--rb-text, #c8cdd2);
		font-family: var(--rb-font, sans-serif);
		font-size: var(--rb-fs-label, 10px);
		pointer-events: none;
	}
</style>
