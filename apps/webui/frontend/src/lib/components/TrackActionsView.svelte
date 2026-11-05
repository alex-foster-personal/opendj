<script lang="ts">
	// Props-only half of TrackActions: the reveal action and each hand-off
	// button are callbacks or computed rows, so the container owns the probe,
	// the engine calls and the click outcome.
	import type { HandoffAppId } from '$lib/rb/api-track-handoff';

	let {
		revealTitle,
		onReveal,
		buttons,
		onOpenIn,
		outcome
	}: {
		revealTitle: string;
		onReveal: () => void;
		buttons: { app: HandoffAppId; label: string; disabled: boolean; title: string }[];
		onOpenIn: (app: HandoffAppId, label: string) => void;
		outcome: { text: string; error: boolean } | null;
	} = $props();
</script>

<p class="track-actions">
	<button type="button" title={revealTitle} onclick={onReveal}> Show in Finder </button>
	{#each buttons as { app, label, disabled, title } (app)}
		<button
			type="button"
			{disabled}
			{title}
			aria-label={disabled ? `${label} - ${title}` : label}
			onclick={() => onOpenIn(app, label)}>{label}</button
		>
	{/each}
	{#if outcome}
		<span
			class="handoff-outcome"
			class:error={outcome.error}
			role={outcome.error ? 'alert' : 'status'}>{outcome.text}</span
		>
	{/if}
</p>

<style>
	.handoff-outcome {
		margin-left: 0.5rem;
		font-size: 0.85em;
		color: var(--muted);
	}
	.handoff-outcome.error {
		color: var(--danger);
	}
</style>
