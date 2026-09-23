<script lang="ts">
	import '../app.css';
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import ToastStack from '$lib/components/rb/ToastStack.svelte';
	import { health, refreshHealth, toasts } from '$lib/stores.svelte';
	import BannerWarning from '$lib/components/BannerWarning.svelte';
	import SettingsOverlay from '$lib/components/settings/SettingsOverlay.svelte';
	import StageOverlay from '$lib/components/lyrics/StageOverlay.svelte';
	import UserBauble from '$lib/components/UserBauble.svelte';
	import CloudSyncStatusChip from '$lib/components/CloudSyncStatusChip.svelte';
	import AccountOverlay from '$lib/components/account/AccountOverlay.svelte';
	import SignInOverlay from '$lib/components/account/SignInOverlay.svelte';
	import HotkeysOverlay from '$lib/components/rb/hotkeys/HotkeysOverlay.svelte';
	import { installHotkeysOverlayHotkeys } from '$lib/components/rb/hotkeys/install-hotkeys-overlay';
	import QuitConfirmOverlay from '$lib/components/shell/QuitConfirmOverlay.svelte';
	import { installQuitGate } from '$lib/shell/quit-gate';
	import PreflightScreen from '$lib/components/preflight/PreflightScreen.svelte';
	import {
		LIBRARY_ATTACHED_CHECK_ID,
		preflightGate,
		shouldBlockOnPreflight
	} from '$lib/preflight/preflight.svelte';
	import { bootGateYielded } from '$lib/overlays/overlay-stack';
	import { needsSetupForEmptyLibrary } from '$lib/preflight/fresh-install';
	import { accountOverlay } from '$lib/account/overlay.svelte';
	import { signInOverlay } from '$lib/auth/sign-in-overlay.svelte';
	import { runFirstRunGate } from '$lib/setup/first-run-gate.svelte';
	import { openSetupOverlay, setupOverlay } from '$lib/setup/overlay.svelte';
	import { settingsOverlay } from '$lib/settings/overlay.svelte';
	import { finalSetupRefusal } from '$lib/setup/setup-api';
	import { SETUP_HOST_ROUTE } from '$lib/setup/run-setup';
	import { installBootLandingRedirect } from '$lib/rb/boot-landing';
	import { readBootStampMirror, touchLastGigAt } from '$lib/rb/last-gig-stamp';
	import { isPerformanceRoutePath } from '$lib/rb/performance-preset';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { startAppInstruments } from '$lib/rb/app-init';
	import { installShellCommandPoll } from '$lib/rb/shell-commands';
	import { installShellNavigationPoll } from '$lib/rb/shell-navigation';
	import { installSettingsHotkeys, openSettings } from '$lib/settings/hotkeys';
	import { connect as connectEventsBus } from '$lib/api/events-bus';
	import { capabilities, progressRefusal } from '$lib/api/capabilities.svelte';
	import { entitlements } from '$lib/api/entitlements.svelte';
	import BuildIdentity from '$lib/components/rb/BuildIdentity.svelte';
	import BrandLaunch from '$lib/components/BrandLaunch.svelte';
	import PerformanceAppNav from '$lib/components/PerformanceAppNav.svelte';

	let { children } = $props();

	/** Why the Progress link goes nowhere useful, or null when it works. */
	const ledgerRefusal = $derived(progressRefusal());

	// /performance is a pixel-faithful full-window rekordbox clone; it must
	// bypass the app shell (sidebar/topbar/padding) - RECON-FRONTEND 5,
	// option (a). Toasts stay global as the app-wide error surface.
	const isPerformance = $derived(isPerformanceRoutePath($page.url.pathname));

	const setupOpen = $derived(setupOverlay.open);
	const yieldBootGate = $derived(
		bootGateYielded({
			setup: setupOpen,
			settings: settingsOverlay.open,
			account: accountOverlay.open,
			signIn: signInOverlay.open
		})
	);
	const blockOnPreflight = $derived(shouldBlockOnPreflight(preflightGate.status, yieldBootGate));
	const hideCheckIds = $derived(
		setupOpen && preflightGate.checks.some((check) => check.id === LIBRARY_ATTACHED_CHECK_ID)
			? [LIBRARY_ATTACHED_CHECK_ID]
			: []
	);
	const showPreflightIndicator = $derived(!blockOnPreflight && !preflightGate.cleared);

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

	// PERFMODE-11: stamp Gig activity at most once per minute without blocking paint.
	$effect(() => {
		if (!isPerformance) return;
		touchLastGigAt();
	});

	/**
	 * THE first-run gate, at the root so there is exactly one of it.
	 *
	 * It used to live on the library page, which meant the ask only existed on
	 * one route and only over an empty table. Setup is now an OVERLAY, so the
	 * gate raises it and puts the performance view behind it -- the app the
	 * user just installed, visible, with one step left.
	 *
	 * The daemon decides, not the browser: `should_show_wizard` is computed
	 * engine-side (and is already false for a developer checkout), so a
	 * reload, a second tab and an agent all get the same answer. The rule
	 * itself lives in $lib/setup/first-run, under test.
	 */
	function openSetupForFirstRun(): void {
		openSetupOverlay();
		// Already on a performance route (the packaged shell's landing
		// route) means no navigation at all; the overlay is simply raised.
		if (!isPerformance) void goto(SETUP_HOST_ROUTE);
	}

	function raiseSetupOnFirstRun(): void {
		void runFirstRunGate().then((show) => {
			if (show !== true) return;
			openSetupForFirstRun();
		});
	}

	// When preflight says the library is empty, open setup even if the daemon
	// suppressed should_show_wizard (e.g. dev checkout) or the first-run probe
	// raced entitlements. Decoupled from entitlements.load().
	$effect(() => {
		if (!needsSetupForEmptyLibrary(preflightGate.checks, setupOpen)) return;
		if (finalSetupRefusal() !== null) return;
		openSetupForFirstRun();
	});

	// Run as soon as the client router is live; onMount alone is too late for
	// domcontentloaded e2e and causes a visible library flash on cold open.
	$effect(() => {
		if (typeof window === 'undefined') return;
		installBootLandingRedirect({
			goto,
			getPathname: () => $page.url.pathname,
			readBootStamp: readBootStampMirror
		});
	});

	onMount(() => {
		// THE capability probe: one health GET, before anything daemon-specific
		// decides whether it is real. Every other surface reads the answer.
		void capabilities.probe();
		// The entitlement set, once, for the same reason: `planRefusal()` is
		// read synchronously by controls all over the app, so the one request
		// behind it fires here rather than per surface. Memoized on success.
		void entitlements.load();
		raiseSetupOnFirstRun();
		refreshHealth();
		const uninstallSettings = installSettingsHotkeys();
		const uninstallHotkeysOverlay = installHotkeysOverlayHotkeys();
		const uninstallQuitGate = installQuitGate();
		// Page-lifetime instruments: usage heartbeat + the DevTools perf log
		// globals the e2e latency floor reads. See $lib/rb/app-init.
		const stopInstruments = startAppInstruments();
		const uninstallShellNavigation = installShellNavigationPoll();
		const uninstallShellCommands = installShellCommandPoll();
		const id = setInterval(refreshHealth, 30_000);
		return () => {
			uninstallSettings();
			uninstallHotkeysOverlay();
			uninstallQuitGate();
			stopInstruments();
			uninstallShellNavigation();
			uninstallShellCommands();
			clearInterval(id);
		};
	});
</script>

<svelte:head>
	<title>Open DJ</title>
</svelte:head>

{#if blockOnPreflight}
	<!-- PREFLIGHT-01 (#771): the boot gate. Nothing else renders until a real
	     `pass` arrives from GET /api/v1/preflight -- no skip/continue-anyway,
	     see PreflightScreen.svelte for the polling policy. While first-run
	     setup is open the gate yields so the wizard is not buried. -->
	<PreflightScreen mode="boot" blocking navigate={goto} hideCheckIds={hideCheckIds} />
{:else}

{#if health.bindWarning}
	<BannerWarning message={health.bindWarning} />
{/if}

{#if isPerformance}
	{@render children()}
	<PerformanceAppNav />
{:else}
<div class="app-shell">
	<aside class="sidebar">
		<h1>Open DJ</h1>
		<nav>
			<a href="/">Library</a>
			<a href="/pairings">Pairings</a>
			<a href="/smartlists">Smartlists</a>
			<a href="/queues">Queues</a>
			<a href="/reconcile">Missing tracks</a>
			<a href="/dedup">Dedup Review</a>
			<a href="/performance">Performance</a>
			<a href="/play-analytics">Play analytics</a>
			<a href="/library-wheel">Library wheel</a>
			<a href="/sets">Sessions / REC</a>
			<a href="/cloudsync">CloudSync</a>
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
			<div class="status-strip" data-testid="header-status-strip">
				{#if health.data}
					<span class="readout readout-numeric" title={String(health.data.state_db.tracks)}>{health.data.state_db.tracks} tracks</span>
					<span class="sep" aria-hidden="true"> · </span>
					<span class="readout readout-numeric" title={String(health.data.state_db.playlists)}>{health.data.state_db.playlists} playlists</span>
					<span class="sep" aria-hidden="true"> · </span>
					{#if health.data.cloud.lock_holder}
						<span class="readout">lock: {health.data.cloud.lock_holder.holder}</span>
					{:else}
						<span class="readout">lock: free</span>
					{/if}
					{#if health.data.syncthing}
						<span class="sep" aria-hidden="true"> · </span>
						<span class="readout">syncthing: {health.data.syncthing.peers_connected} peers - {health.data.syncthing.folder_state}</span>
					{/if}
					<span class="sep" aria-hidden="true"> · </span>
					<span class="readout">bind: {health.data.bind_host}</span>
				{:else}
					<span class="readout">Connecting to configured worktree daemon...</span>
				{/if}
			</div>
			<CloudSyncStatusChip />
			<UserBauble />
		</div>
		<div class="content">
			{@render children()}
		</div>
	</main>
	<!-- The app shell's bottom tray. It exists so the build identity has a
	     place of its own instead of floating over the sidebar, and it spans
	     both columns so the chip sits in the window's bottom RIGHT corner.
	     /performance has its own tray (the browser panel's bottom bar) and
	     mounts the chip there, so exactly one is ever on screen. -->
	<footer class="app-tray" aria-label="status tray">
		<BuildIdentity />
	</footer>
</div>
{/if}

{/if}

{#if showPreflightIndicator}
	<PreflightScreen mode="boot" blocking={false} navigate={goto} hideCheckIds={hideCheckIds} />
{/if}

<SettingsOverlay />
<StageOverlay />
<!-- The first-run wizard, over whatever route is on screen. Mounted at the
     root for the same reason SettingsOverlay is: /performance bypasses the app
     shell, and the one surface a brand new user meets cannot be missing there
     of all places. Imported lazily (the payback named in
     scripts/bundle-budget.mjs): the wizard renders only while the overlay is
     open, and its own effects early-return while closed, so keeping it off the
     first paint changes nothing a user or an agent can observe. -->
{#if setupOpen}
	{#await import('$lib/components/setup/SetupOverlay.svelte') then { default: SetupOverlay }}
		<SetupOverlay />
	{/await}
{/if}
<!-- The account panel, mounted at the root for the same reason as the two
     above: the user bauble is drawn on /performance too, and its Account door
     must open something there. -->
<AccountOverlay />
<SignInOverlay />
<!-- Hotkeys overlay (LIBUX-04): "/" hold and "?" toggle. Mounted at the root
     for the same reason SettingsOverlay is: /performance bypasses the app
     shell, and the cheatsheet has to work there too. -->
<HotkeysOverlay />
<QuitConfirmOverlay />
<!-- The diagnostics consent dialog (OBS-05) is mounted by $lib/telemetry-consent
     from a deferred boot task, so neither it nor its module is on the
     first-paint path or in the library page's bundle budget. -->

<ToastStack items={toasts} />
<BrandLaunch />

<style>
	/* One extra row for the tray. Declared here rather than in app.css so the
	   blast radius of the tray is this component; Svelte's scoping class wins
	   over the base rule on specificity. */
	.app-shell {
		grid-template-rows: minmax(0, 1fr) auto;
	}
	.app-shell > .sidebar,
	.app-shell > main {
		grid-row: 1;
	}
	.app-tray {
		grid-column: 1 / -1;
		display: flex;
		align-items: center;
		min-height: 20px;
		padding: 0 0.35rem;
		background: var(--surface);
		border-top: 1px solid var(--border);
	}
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
	.status-strip {
		flex: 1 1 auto;
		min-width: 0;
		display: flex;
		flex-wrap: wrap;
		align-items: center;
	}
	.status-strip .readout {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		max-width: 100%;
	}
	.status-strip .sep {
		flex: none;
		white-space: pre;
	}
</style>
