<script lang="ts">
	// RESCUE-07: decks that were playing before a reload come back stopped.
	// Say so and offer one click (which also satisfies the browser's audio
	// gesture rule) instead of leaving the operator to notice the silence.
	import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
	import {
		acceptReloadResume,
		dismissReloadResume,
		reloadResume
	} from '$lib/rb/reload-resume.svelte';
	import { uiPrefs } from '$lib/rb/prefs.svelte';

	import { deckStates } from '$lib/player/state.svelte';

	let busy = $state(false);

	// Started by hand (or by an agent's `play`): the offer has been answered.
	$effect(() => {
		const offer = reloadResume.offer;
		if (offer !== null && !busy && offer.decks.every((deck) => deckStates[deck].playing)) {
			dismissReloadResume();
		}
	});

	async function _resume(): Promise<void> {
		busy = true;
		try {
			await acceptReloadResume(dispatchPerformanceCommand);
		} finally {
			busy = false;
		}
	}
</script>

{#if reloadResume.offer !== null}
	<div class="reload-resume-banner" role="alert" data-testid="reload-resume-banner">
		<span class="headline">
			{reloadResume.offer.decks.length === 1
				? `Deck ${reloadResume.offer.decks[0]} was playing before the reload and is stopped.`
				: `Decks ${reloadResume.offer.decks.join(', ')} were playing before the reload and are stopped.`}
			{#if uiPrefs.auto_play_enabled}
				AutoPlay is on but cannot queue anything until a deck plays.
			{/if}
		</span>
		{#if reloadResume.error !== null}
			<span class="error">{reloadResume.error}</span>
		{/if}
		<button
			type="button"
			class="resume"
			disabled={busy}
			title="Start the stopped decks again; the first one takes master"
			data-testid="reload-resume-accept"
			onclick={_resume}
		>
			Resume {reloadResume.offer.decks.length === 1 ? 'deck' : `${reloadResume.offer.decks.length} decks`}
		</button>
		<button
			type="button"
			class="dismiss"
			disabled={busy}
			title="Keep the decks stopped"
			data-testid="reload-resume-dismiss"
			onclick={dismissReloadResume}
		>
			Keep stopped
		</button>
	</div>
{/if}

<style>
	/* Fixed, outside the grid, like the PLAY-08 stall banner: it must not reflow the decks. */
	.reload-resume-banner {
		position: fixed;
		top: var(--rb-topbar-h);
		left: 0;
		right: 0;
		z-index: 41;
		display: flex;
		align-items: baseline;
		justify-content: center;
		gap: 10px;
		flex-wrap: wrap;
		padding: 4px 10px;
		border-bottom: 1px solid color-mix(in srgb, var(--rb-orange) 65%, var(--rb-border));
		background: color-mix(in srgb, var(--rb-orange) 18%, var(--rb-panel));
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
		line-height: 16px;
	}
	.headline {
		font-weight: 600;
	}
	.error {
		color: var(--rb-red);
	}
	button {
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		padding: 0 8px;
		background: var(--rb-panel-raised);
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
		cursor: pointer;
	}
	.resume {
		font-weight: 600;
	}
</style>
