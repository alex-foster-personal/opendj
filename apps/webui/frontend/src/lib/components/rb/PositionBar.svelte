<script lang="ts">
	let {
		positionMs = 0,
		durationMs = null as number | null,
		label = 'Track position'
	}: {
		positionMs?: number;
		durationMs?: number | null;
		label?: string;
	} = $props();

	const pct = $derived(
		durationMs !== null && durationMs > 0
			? Math.min(100, Math.max(0, (positionMs / durationMs) * 100))
			: 0
	);
</script>

<div class="position-bar" data-testid="trackify-position-bar" aria-label={label}>
	<div
		class="position-fill"
		style={`width: ${pct}%`}
		title={`${Math.round(positionMs)} ms of ${durationMs ?? 'unknown'} ms`}
	></div>
</div>

<style>
	.position-bar {
		width: 100%;
		height: 0.35rem;
		border-radius: 999px;
		background: color-mix(in srgb, var(--text) 18%, transparent);
		overflow: hidden;
	}
	.position-fill {
		height: 100%;
		background: var(--accent, #4ea1ff);
		transition: width 120ms linear;
	}
</style>
