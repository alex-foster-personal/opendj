<script lang="ts">
	import '../app.css';
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { health, refreshHealth, toasts } from '$lib/stores.svelte';
	import BannerWarning from '$lib/components/BannerWarning.svelte';

	let { children } = $props();

	// /performance is a pixel-faithful full-window rekordbox clone; it must
	// bypass the app shell (sidebar/topbar/padding) - RECON-FRONTEND 5,
	// option (a). Toasts stay global as the app-wide error surface.
	const isPerformance = $derived($page.url.pathname === '/performance');

	onMount(() => {
		refreshHealth();
		const id = setInterval(refreshHealth, 30_000);
		return () => clearInterval(id);
	});
</script>

{#if health.bindWarning}
	<BannerWarning message={health.bindWarning} />
{/if}

{#if isPerformance}
	{@render children()}
{:else}
<div class="app-shell">
	<aside class="sidebar">
		<h1>music-dj-tools</h1>
		<nav>
			<a href="/">Library</a>
			<a href="/pairings">Pairings</a>
			<a href="/queues">Queues</a>
			<a href="/performance">Performance</a>
		</nav>
	</aside>
	<main>
		<div class="topbar">
			{#if health.data}
				<span>{health.data.state_db.tracks} tracks · {health.data.state_db.playlists} playlists</span>
				{#if health.data.cloud.lock_holder}
					<span>lock: {health.data.cloud.lock_holder.holder}</span>
				{:else}
					<span>lock: free</span>
				{/if}
				<span>bind: {health.data.bind_host}</span>
			{:else}
				<span>Connecting to daemon at :8585...</span>
			{/if}
		</div>
		<div class="content">
			{@render children()}
		</div>
	</main>
</div>
{/if}

<div class="toast-stack">
	{#each toasts as toast (toast.id)}
		<div class="toast" class:error={toast.kind === 'error'}>{toast.message}</div>
	{/each}
</div>
