<script lang="ts">
	// LIBWS-01: Library mode's default view is the Crate Desk
	// (lib/components/library/CrateDesk.svelte). The root layout renders this
	// route without the app shell (see +layout.svelte).
	//
	// The desk sits behind a dynamic import on purpose: it carries the whole
	// shared browser, and every cold open lands on "/" before PERFMODE-11
	// redirects it to Gig, so a static import would make every boot download
	// it twice over the library budget (scripts/bundle-budget.mjs). Behind the
	// import its browser chunks are the ones /performance already pays for.
	//
	// No first-run gate here, deliberately: that is ONE gate in the root
	// layout (raiseSetupOnFirstRun), and a second copy would be a second
	// decision about the same thing.
	import { onMount, type Component } from 'svelte';
	import { enterLibraryWorkspace, leaveLibraryWorkspace } from '$lib/library/library-workspace.svelte';

	// Set before the browser's first render so its deck guards hold from the
	// first frame, not from after mount.
	enterLibraryWorkspace();

	let CrateDesk: Component | null = $state(null);
	let loadError: string | null = $state(null);

	onMount(() => {
		let cancelled = false;
		import('$lib/components/library/CrateDesk.svelte').then(
			(mod) => {
				if (!cancelled) CrateDesk = mod.default;
			},
			(error: unknown) => {
				if (!cancelled) loadError = error instanceof Error ? error.message : String(error);
			}
		);
		return () => {
			cancelled = true;
			leaveLibraryWorkspace();
		};
	});
</script>

<svelte:head>
	<title>Open DJ - Library</title>
</svelte:head>

{#if CrateDesk}
	<CrateDesk />
{:else if loadError !== null}
	<p class="desk-status desk-error" role="alert" data-testid="library-workspace-error">
		The library view failed to load: {loadError}. Reload the page to retry.
	</p>
{:else}
	<p class="desk-status" data-testid="library-workspace-loading">Loading library...</p>
{/if}

<style>
	.desk-status {
		position: fixed;
		inset: 0;
		display: grid;
		place-items: center;
		margin: 0;
		background: #0a0c0f;
		color: #8a94a3;
		font: 12px system-ui, sans-serif;
	}
	.desk-error {
		color: #ff6b6b;
	}
</style>
