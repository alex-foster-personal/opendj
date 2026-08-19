<script lang="ts">
	import '../app.css';
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { health, refreshHealth, toasts } from '$lib/stores.svelte';
	import BannerWarning from '$lib/components/BannerWarning.svelte';
	import SettingsOverlay from '$lib/components/settings/SettingsOverlay.svelte';
	import { isPerformanceRoutePath } from '$lib/rb/performance-preset';
	import { hydrateConfirmPrefsFromDisk, uiPrefs } from '$lib/rb/prefs.svelte';
	import { installSettingsHotkeys, openSettings } from '$lib/settings/hotkeys';
	import { connect as connectEventsBus } from '$lib/api/events-bus';
	import { capabilities, progressRefusal } from '$lib/api/capabilities.svelte';

	let { children } = $props();

	/** Why the Progress link goes nowhere useful, or null when it works. */
	const ledgerRefusal = $derived(progressRefusal());

	// /performance is a pixel-faithful full-window rekordbox clone; it must
	// bypass the app shell (sidebar/topbar/padding) - RECON-FRONTEND 5,
	// option (a). Toasts stay global as the app-wide error surface.
	const isPerformance = $derived(isPerformanceRoutePath($page.url.pathname));

	// Keep html[data-theme] in sync (prefs module also applies on load/set).
	$effect(() => {
		if (typeof document === 'undefined') return;
		document.documentElement.dataset.theme = uiPrefs.theme;
		document.documentElement.style.colorScheme = uiPrefs.theme;
	});

	// The events socket exists on the engine only. Connecting against a legacy
	// boot buys nothing but a reconnect loop against a route that is not there,
	// so the bus waits for the probe. connect() is idempotent, and this effect
	// re-runs if a later probe finally identifies an engine, so a daemon that
	// was down at page load still gets a bus without any polling here.
	$effect(() => {
		if (!capabilities.events) return;
		// One bus for the page lifetime: never torn down on navigation.
		connectEventsBus();
	});

	onMount(() => {
		// THE capability probe: one health GET, before anything daemon-specific
		// decides whether it is real. Every other surface reads the answer.
		void capabilities.probe();
		refreshHealth();
		void hydrateConfirmPrefsFromDisk();
		const uninstallSettings = installSettingsHotkeys();
		const id = setInterval(refreshHealth, 30_000);
		return () => {
			uninstallSettings();
			clearInterval(id);
		};
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
			<a href="/smartlists">Smartlists</a>
			<a href="/queues">Queues</a>
			<a href="/reconcile">Missing tracks</a>
			<a href="/dedup">Dedup Review</a>
			<a href="/performance">Performance</a>
			<a href="/play-analytics">Play analytics</a>
			<a href="/sets">Sessions / REC</a>
			<!-- Ledger route: legacy-daemon only, so the link says so rather than
			     leading to a page that can only apologise. -->
			<a
				href="/progress-tree"
				class:nav-unavailable={ledgerRefusal !== null}
				title={ledgerRefusal ?? 'Fan-out progress ledger (GET /api/v1/progress)'}
			>
				Progress
			</a>
			<a href="/admin">Admin</a>
			<a href="/settings">Settings (daemon)</a>
			<button type="button" class="nav-settings" onclick={() => openSettings()}>
				Settings (Cmd+,)
			</button>
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
				{#if health.data.syncthing}
					<span>sync: {health.data.syncthing.peers_connected} peers - {health.data.syncthing.folder_state}</span>
				{:else}
					<span title="not implemented - see PARITY-TODO (syncthing not configured)">sync: n/a</span>
				{/if}
				<span>bind: {health.data.bind_host}</span>
			{:else}
				<span>Connecting to configured worktree daemon...</span>
			{/if}
		</div>
		<div class="content">
			{@render children()}
		</div>
	</main>
</div>
{/if}

<SettingsOverlay />

<div class="toast-stack">
	{#each toasts as toast (toast.id)}
		<div class="toast" class:error={toast.kind === 'error'}>{toast.message}</div>
	{/each}
</div>

<style>
	.nav-settings {
		display: block;
		width: 100%;
		margin-top: 0.15rem;
		padding: 0.2rem 0;
		border: none;
		background: transparent;
		color: var(--accent);
		text-align: left;
		font: inherit;
		cursor: pointer;
	}
	.nav-settings:hover {
		text-decoration: underline;
	}
	/* Still navigable (the page explains itself), just visibly not on offer. */
	.nav-unavailable {
		opacity: 0.45;
	}
</style>
