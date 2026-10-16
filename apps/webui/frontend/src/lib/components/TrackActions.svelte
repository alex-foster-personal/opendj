<script lang="ts">
	import { onMount } from 'svelte';
	import { REVEAL_TRACK_TITLE, runRevealTracks } from '$lib/components/rb/browser/track-context-menu';
	import {
		fetchHandoffApps,
		HANDOFF_BUTTONS,
		handoffButtonState,
		openTrackInApp,
		type HandoffApp,
		type HandoffAppId
	} from '$lib/rb/api-track-handoff';

	let { stableId }: { stableId: string } = $props();

	// HANDOFF-01..03: each button is enabled only when the engine found that
	// app on this machine; otherwise its tooltip says why, never a dead click.
	let probe = $state<HandoffApp[] | 'loading' | { error: string }>('loading');

	onMount(() => {
		fetchHandoffApps()
			.then((apps) => (probe = apps))
			.catch((err: unknown) => (probe = { error: err instanceof Error ? err.message : String(err) }));
	});

	// The click's result shows inline beside the buttons (a status line, not a
	// toast, so this component stays off the stores.svelte hub).
	let outcome = $state<{ text: string; error: boolean } | null>(null);

	async function openIn(app: HandoffAppId, label: string): Promise<void> {
		outcome = null;
		try {
			await openTrackInApp(stableId, app);
			outcome = { text: `Sent to ${label.replace('Open in ', '')}`, error: false };
		} catch (err) {
			outcome = { text: err instanceof Error ? err.message : String(err), error: true };
		}
	}
</script>

<p class="track-actions">
	<button type="button" title={REVEAL_TRACK_TITLE} onclick={() => void runRevealTracks([stableId])}>
		Show in Finder
	</button>
	{#each HANDOFF_BUTTONS as { app, label } (app)}
		{@const state = handoffButtonState(app, probe)}
		<button
			type="button"
			disabled={state.disabled}
			title={state.title}
			aria-label={state.disabled ? `${label} - ${state.title}` : label}
			onclick={() => void openIn(app, label)}>{label}</button
		>
	{/each}
	{#if outcome}
		<span class="handoff-outcome" class:error={outcome.error} role={outcome.error ? 'alert' : 'status'}
			>{outcome.text}</span
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
