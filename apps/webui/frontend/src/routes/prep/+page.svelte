<script lang="ts">
	/**
	 * /prep - Prep mode (PREPWS-01).
	 *
	 * Until Prep has a workspace of its own, this page is a hub of the prep
	 * pages that already work, so choosing Prep in the mode chooser lands
	 * somewhere useful instead of on a stub. Every link is live; nothing here
	 * is inert or mocked.
	 */
	import { PREP_HUB_ENTRIES } from '$lib/shell/prep-hub';
</script>

<svelte:head>
	<title>Prep - Open DJ</title>
</svelte:head>

<main class="prep-hub" data-testid="prep-hub">
	<h1>Prep</h1>
	<p class="lede">Get your library ready before the gig. Pick what you want to work on.</p>
	<ul class="prep-hub-list">
		{#each PREP_HUB_ENTRIES as entry (entry.href)}
			<li>
				<a href={entry.href} data-testid="prep-hub-link">
					<span class="label">{entry.label}</span>
					<span class="does">{entry.does}</span>
				</a>
			</li>
		{/each}
	</ul>
	<p class="note">
		A single Prep workspace that brings these together is planned. Until then each one opens
		its own page.
	</p>
</main>

<style>
	.prep-hub {
		padding: 2rem;
		max-width: 44rem;
	}
	.lede {
		color: var(--muted);
		margin: 0.25rem 0 1.25rem;
	}
	.prep-hub-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		gap: 0.5rem;
	}
	.prep-hub-list a {
		display: grid;
		gap: 0.15rem;
		padding: 0.75rem 1rem;
		border: 1px solid var(--border);
		border-radius: 6px;
		color: inherit;
		text-decoration: none;
	}
	.prep-hub-list a:hover,
	.prep-hub-list a:focus-visible {
		border-color: var(--accent);
	}
	.label {
		font-weight: 600;
	}
	.does {
		color: var(--muted);
		font-size: 0.9em;
	}
	.note {
		color: var(--muted);
		font-size: 0.85em;
		margin-top: 1.25rem;
	}
</style>
