<script lang="ts">
	/**
	 * /setup: a DOOR into the setup overlay, not a page of its own.
	 */
	import { onMount } from 'svelte';

	import { goto } from '$app/navigation';
	import { beginSoftNavigationHandoff } from '$lib/api/lifecycle-abort';
	import { page } from '$app/stores';
	import { openSetupOverlay } from '$lib/setup/overlay.svelte';
	import { SETUP_HOST_ROUTE } from '$lib/setup/run-setup';
	import { preserveUrlSearchOnRedirect } from '$lib/setup/wizard-rules';

	onMount(() => {
		openSetupOverlay();
		const endHandoff = beginSoftNavigationHandoff();
		void goto(preserveUrlSearchOnRedirect(SETUP_HOST_ROUTE, $page.url.search), {
			replaceState: true
		}).finally(() => {
			queueMicrotask(endHandoff);
		});
	});
</script>

<svelte:head>
	<title>Setup - music-dj-tools</title>
</svelte:head>

<section class="setup-door" aria-label="First-run setup">
	<h2>First-run setup</h2>
	<p role="status">Opening setup over the app...</p>
</section>

<style>
	.setup-door {
		max-width: 40rem;
	}
</style>
