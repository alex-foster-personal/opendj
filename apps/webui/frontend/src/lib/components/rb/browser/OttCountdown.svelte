<script lang="ts">
	// ~10px circle countdown for temporary UI windows (genre filter, etc.).
	// Pure presentational: parent owns until/total via OttTimer.
	let {
		untilMs = 0,
		totalMs = 0,
		label = 'temporary UI window'
	}: {
		untilMs?: number;
		totalMs?: number;
		label?: string;
	} = $props();

	let now = $state(Date.now());

	$effect(() => {
		if (untilMs <= 0) return;
		now = Date.now();
		const id = setInterval(() => {
			now = Date.now();
		}, 100);
		return () => clearInterval(id);
	});

	const remaining = $derived(untilMs > 0 ? Math.max(0, untilMs - now) : 0);
	const fraction = $derived(
		totalMs > 0 && remaining > 0 ? Math.min(1, remaining / totalMs) : 0
	);
	const deg = $derived(fraction * 360);
	const secs = $derived(Math.ceil(remaining / 1000));
	const tip = $derived(
		remaining > 0
			? `${secs}s left - ${label}`
			: label
	);
</script>

{#if remaining > 0}
	<span
		class="ott"
		role="timer"
		aria-label={tip}
		title={tip}
		style={`background: conic-gradient(var(--rb-accent, #e8a13a) ${deg}deg, color-mix(in srgb, var(--rb-text-dim, #888) 35%, transparent) 0)`}
	></span>
{/if}

<style>
	.ott {
		display: inline-block;
		width: 10px;
		height: 10px;
		border-radius: 50%;
		flex: none;
		box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--rb-border, #333) 80%, transparent);
		pointer-events: auto;
	}
</style>
