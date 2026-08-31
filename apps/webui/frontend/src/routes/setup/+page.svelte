<script lang="ts">
	/**
	 * /setup: a DOOR into the setup overlay, not a page of its own.
	 *
	 * The wizard used to live here as a standalone screen. It is now
	 * $lib/components/setup/SetupOverlay, mounted from the root layout and
	 * drawn over the performance view, because a first run should arrive with
	 * the app behind it rather than instead of it.
	 *
	 * This route stays because deleting it would 404 three things that already
	 * exist in the wild: bookmarks, the shared SETUP_ROUTE every entry point
	 * navigates to ($lib/setup/run-setup), and agent flows that were told
	 * "open /setup". All three now land on the host route with the overlay
	 * raised, which is the same wizard in its new home.
	 *
	 * `replaceState` on purpose: this route is never a place to come BACK to,
	 * so leaving it in history would give Back a stop that instantly bounces
	 * forward again.
	 *
	 * Requirements (mini-PRD):
	 *   ✔︎ ✅ 🎯 /setup renders something honest and then lands on the host
	 *     route with the overlay open. It never 404s and never dead-ends.
	 *     [if] /setup renders a wizard of its own [then ⛔️] two wizards
	 *   ✔︎ ✅ 🎯 the destination is SETUP_HOST_ROUTE, spelled once.
	 *     [if] a literal '/performance' appears here [then ⛔️] broken
	 */
	import { onMount } from 'svelte';

	import { goto } from '$app/navigation';
	import { openSetupOverlay } from '$lib/setup/overlay.svelte';
	import { SETUP_HOST_ROUTE } from '$lib/setup/run-setup';

	onMount(() => {
		openSetupOverlay();
		void goto(SETUP_HOST_ROUTE, { replaceState: true });
	});
</script>

<svelte:head>
	<title>Setup - music-dj-tools</title>
</svelte:head>

<section class="setup-door" aria-label="First-run setup">
	<h2>First-run setup</h2>
	<p role="status">
		Setup now opens over the app itself. Taking you to
		<code>{SETUP_HOST_ROUTE}</code> with the wizard on top...
	</p>
	<p class="muted">
		Every step is an HTTP endpoint under <code>/api/v1/setup</code>, so this
		flow can also be driven without a browser.
	</p>
</section>

<style>
	.setup-door {
		max-width: 40rem;
	}
	.muted {
		color: var(--muted);
		font-size: 0.85rem;
	}
</style>
