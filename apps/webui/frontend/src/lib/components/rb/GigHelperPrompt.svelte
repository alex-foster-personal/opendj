<script lang="ts">
	import {
		dismissGigHelperPrompt,
		gigHelperPromptVisible
	} from '$lib/rb/gig-helper-prompt.svelte';
	import { setGigHelper } from '$lib/rb/prefs.svelte';

	function enableGigHelper(): void {
		setGigHelper('on');
		dismissGigHelperPrompt();
	}

	function deferGigHelper(): void {
		dismissGigHelperPrompt();
	}

	function declineGigHelper(): void {
		setGigHelper('off');
		dismissGigHelperPrompt();
	}
</script>

{#if gigHelperPromptVisible}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="gh-backdrop" role="presentation" onclick={() => deferGigHelper()}>
		<div
			class="gh-panel"
			role="dialog"
			aria-modal="true"
			aria-label="Enable Gig helper?"
			data-testid="gig-helper-prompt"
			tabindex="-1"
			onclick={(event) => event.stopPropagation()}
		>
			<h2 class="gh-title">Enable Gig helper?</h2>
			<p class="gh-body">
				Gig helper monitors system pressure and keeps Gig background caps active while you perform.
				A native menubar helper like Butler is planned later; this v1 stays inside Open DJ.
			</p>
			<div class="gh-actions">
				<button type="button" class="gh-btn" onclick={() => declineGigHelper()}>No thanks</button>
				<button type="button" class="gh-btn" onclick={() => deferGigHelper()}>Not now</button>
				<button type="button" class="gh-btn gh-primary" onclick={() => enableGigHelper()}>
					Enable
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.gh-backdrop {
		position: fixed;
		inset: 0;
		z-index: 10000;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgb(0 0 0 / 0.55);
	}
	.gh-panel {
		min-width: 18rem;
		max-width: 26rem;
		padding: 1rem 1.25rem;
		border: 1px solid var(--rb-border, #444);
		border-radius: 6px;
		background: var(--rb-bg, #1b1b1b);
		color: var(--rb-text, #eee);
	}
	.gh-title {
		margin: 0;
		font-size: 0.95rem;
	}
	.gh-body {
		margin: 0.5rem 0 1rem;
		font-size: 0.75rem;
		line-height: 1.4;
		color: var(--rb-text-muted, #aaa);
	}
	.gh-actions {
		display: flex;
		flex-wrap: wrap;
		justify-content: flex-end;
		gap: 0.35rem;
	}
	.gh-btn {
		font: inherit;
		font-size: 0.72rem;
		padding: 0.3rem 0.65rem;
		border: 1px solid var(--rb-border, #444);
		border-radius: 4px;
		background: transparent;
		color: inherit;
		cursor: pointer;
	}
	.gh-primary {
		border-color: var(--rb-accent, #4af);
		color: var(--rb-accent, #4af);
		font-weight: 700;
	}
</style>
