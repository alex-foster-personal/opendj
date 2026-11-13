<script lang="ts">
	import '../app.css';
	import '$lib/audio-engine/engine-bridge';
	import { onMount, setContext } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import ToastStack from '$lib/components/rb/ToastStack.svelte';
	import { announcer } from '$lib/a11y/announce';
	import { health, pushToast, refreshHealth, TOAST_DEFAULT_MS, toasts } from '$lib/stores.svelte';
	import { selectVisibleToasts } from '$lib/toast-tray-policy';
	import BannerWarning from '$lib/components/BannerWarning.svelte';
	import {
		isSettingsOverlayOpen,
		loadSettingsOverlay,
		prefetchSettingsOverlay
	} from '$lib/components/settings/settings-overlay-loader';
	import {
		isStageOverlayOpen,
		loadStageOverlay,
		prefetchStageOverlay
	} from '$lib/components/lyrics/stage-overlay-loader';
	import UserBauble from '$lib/components/UserBauble.svelte';
	import CloudSyncStatusChip from '$lib/components/CloudSyncStatusChip.svelte';
	import AccountOverlay from '$lib/components/account/AccountOverlay.svelte';
	import SignInOverlay from '$lib/components/account/SignInOverlay.svelte';
	import {
		installHotkeysOverlayHotkeys,
		isHotkeysOverlayOpen,
		loadHotkeysOverlay,
		prefetchHotkeysOverlay
	} from '$lib/components/rb/hotkeys/install-hotkeys-overlay';
	import { deferSetNotesHost } from '$lib/set-notes/loader';
	import QuitConfirmOverlay from '$lib/components/shell/QuitConfirmOverlay.svelte';
	import { ensureEarlyShellQuitGate, installQuitGate } from '$lib/shell/quit-gate';

	ensureEarlyShellQuitGate();
	import PreflightScreen from '$lib/components/preflight/PreflightScreen.svelte';
	import {
		LIBRARY_ATTACHED_CHECK_ID,
		checkPreflight,
		preflightGate,
		shouldBlockOnPreflight
	} from '$lib/preflight/preflight.svelte';
	import { bootGateYielded } from '$lib/overlays/overlay-stack';
	import { shouldAutoOpenEmptyLibrarySetup } from '$lib/preflight/fresh-install';
	import { shouldShowPreflightStrip } from '$lib/preflight/boot-copy';
	import { accountOverlay } from '$lib/account/overlay.svelte';
	import { signInOverlay } from '$lib/auth/sign-in-overlay.svelte';
	import { runFirstRunGate } from '$lib/setup/first-run-gate.svelte';
	import { openSetupOverlay, setupOverlay } from '$lib/setup/overlay.svelte';
	import { settingsOverlay } from '$lib/settings/overlay.svelte';
	import { finalSetupRefusal } from '$lib/setup/setup-api';
	import { SETUP_HOST_ROUTE, SETUP_ROUTE } from '$lib/setup/run-setup';
	import { preserveUrlSearchOnRedirect } from '$lib/setup/wizard-rules';
	import { installBootLandingRedirect } from '$lib/rb/boot-landing';
	import { readBootStampMirror, touchLastGigAt } from '$lib/rb/last-gig-stamp';
	import {
		isLibraryWorkspaceRoutePath,
		isSecondScreenRoutePath,
		isPerformanceRoutePath,
		isTrackifyRoutePath,
		rootLayoutRunsMainWindowLoops
	} from '$lib/rb/performance-preset';
	import { refreshReduceMotionFromDisk, setAppMode, uiPrefs } from '$lib/rb/prefs.svelte';
	import {
		applyReducedMotionAttr,
		setReduceMotionPref,
		watchReducedMotionMedia
	} from '$lib/motion/reduced-motion';
	import {
		headerPlaylistCountTitle,
		headerTrackCountTitle,
		isCurrentNavLink,
		SHELL_NAV_CONTEXT,
		sidebarNavLinks,
		type ShellNav
	} from '$lib/shell/sidebar-nav';
	import { startAppInstruments } from '$lib/rb/app-init';
	import { installShellCommandPoll } from '$lib/rb/shell-commands';
	import { installShellNavigationPoll } from '$lib/rb/shell-navigation';
	import { backstageEnabled } from '$lib/shell/backstage-flag';
	import { buildFlags } from '$lib/api/store-build.svelte';
	import { cmdkV2Enabled } from '$lib/rb/cmdk-flag';
	import { installSettingsHotkeys } from '$lib/settings/hotkeys';
	import {
		isRunButtonActionPending,
		runButtonAction,
		runButtonActionGate
	} from '$lib/ui/run-button-action';
	import { connect as connectEventsBus, subscribeKind } from '$lib/api/events-bus';
	import { capabilities, progressRefusal } from '$lib/api/capabilities.svelte';
	import { entitlements } from '$lib/api/entitlements.svelte';
	import BuildIdentity from '$lib/components/rb/BuildIdentity.svelte';
	import BrandLaunch from '$lib/components/BrandLaunch.svelte';
	import type { Component } from 'svelte';
	import { deferFeedbackPinShell } from '$lib/rb/feedback-pin-shell-boot';
	import { bootScheduler } from '$lib/rb/boot-scheduler';
	import { deferLoad } from '$lib/assistant/sidebar-toggle';
	import FeedbackPinTopbarControls from '$lib/components/rb/FeedbackPinTopbarControls.svelte';
	import { installSingleHoverTooltip } from '$lib/ui/single-hover-tooltip';

	const visibleToasts = $derived(selectVisibleToasts(toasts));

	let { children } = $props();

	// The comment-pin layer (markers, the reopened card, the draft bubble) and
	// the topbar pin button are imported inside a deferred boot task, like the
	// consent dialog: static imports charged them, the feedback store and its
	// helpers to the library page's first-paint budget (#3892 put it at 268,926
	// against 258,048). Nothing is lost by the wait: the layer is what hydrates
	// the store, and until it has, the store reports 'unknown' and arming is a
	// no-op, so the button could only have said "probing" and done nothing.
	// The `m` hotkey is installed by the same task for the same reason: arming
	// is a no-op before the layer has hydrated the store, so an earlier
	// listener could only have swallowed the key and done nothing with it.
	let FeedbackPinLayer: Component | null = $state(null);
	let FeedbackPinShellButton: Component | null = $state(null);
	let FeedbackDock: Component | null = $state(null);
	/** Why the pin shell never arrived, or null while it is loading or loaded. */
	let pinShellError: string | null = $state(null);

	/** Why the Progress link goes nowhere useful, or null when it works. */
	const ledgerRefusal = $derived(progressRefusal());
	const navLinks = $derived(sidebarNavLinks(uiPrefs.show_dev_ui));
	/** shell.backstage (default OFF): the Backstage sidebar replaces the flat list. */
	const backstageOn = $derived(backstageEnabled());
	// Lazy so the flag-off default keeps the "library" initial-load bundle budget.
	let BackstageNav = $state<typeof import('$lib/shell/BackstageNav.svelte').default>();
	$effect(() => {
		if (backstageOn) void import('$lib/shell/BackstageNav.svelte').then((m) => (BackstageNav = m.default));
	});
	// #9467: the v2 Cmd K palette on every route. Lazy so neither the bar nor its
	// wiring is charged to any route's first load; loads only once cmdk.v2 is declared on.
	let CommandPaletteHost = $state<
		typeof import('$lib/components/rb/CommandPaletteHost.svelte').default | null
	>(null);
	let commandPaletteRequested = false;
	$effect(() => {
		if (commandPaletteRequested || (buildFlags.loaded && !cmdkV2Enabled())) return;
		commandPaletteRequested = true;
		void import('$lib/components/rb/CommandPaletteHost.svelte').then(
			(m) => {
				CommandPaletteHost = m.default;
			},
			(error: unknown) => {
				console.warn('[cmdk] palette failed to load', error);
			}
		);
	});
	setContext<ShellNav>(SHELL_NAV_CONTEXT, {
		get links() {
			return navLinks;
		},
		selectMode: setAppMode
	});

	// /performance is a pixel-faithful full-window rekordbox clone; it must
	// bypass the app shell (sidebar/topbar/padding) - RECON-FRONTEND 5,
	// option (a). Toasts stay global as the app-wide error surface.
	const isPerformance = $derived(isPerformanceRoutePath($page.url.pathname));
	const isTrackify = $derived(isTrackifyRoutePath($page.url.pathname));
	// LIBWS-01: Library mode's Crate Desk is full-window too, on the same
	// browser chrome as Gig.
	const isLibraryWorkspace = $derived(isLibraryWorkspaceRoutePath($page.url.pathname));
	const isSecondScreen = $derived(isSecondScreenRoutePath($page.url.pathname));
	const isFullBleedRoute = $derived(isPerformance || isTrackify || isLibraryWorkspace || isSecondScreen);

	/** The assistant's door, loaded by a deferred boot task; see the template note. */
	let AgentSidebarMount: typeof import('$lib/components/assistant/AgentSidebarMount.svelte').default | null =
		$state(null);

	const setupOpen = $derived(setupOverlay.open);
	// The wizard component also draws the "Setup incomplete" note, which lives
	// in the state AFTER a close (`open` false, `incomplete` true), so the
	// mount guard below has to cover that state or the note can never render
	// (Codex review of #3862 at 1ac37b0c, P2).
	const setupMounted = $derived(setupOverlay.open || setupOverlay.incomplete);
	// The first-run wizard is a separate chunk (see the bundle-budget note on
	// SetupOverlay), and the request for it starts only when the wizard
	// mounts: a normal library boot never fetches it, which is what the
	// budget's library surface (the files first paint downloads) measures. An
	// import() at script level would start the fetch on every boot before
	// `setupMounted` was consulted (Codex review of #3862 at 1fb0cc41, P1).
	// The promise is kept once made, so a close and reopen awaits the same
	// load rather than a second request.
	//
	// A chunk that cannot be fetched (a stale client after a deploy, a dropped
	// connection) must not strand first run: `setupOpen` makes the preflight
	// gate yield to the wizard, so a rejected promise left in place would show
	// nothing at all, and reopening would await the same rejection. So the
	// failure is reported the way the pin shell's is (an error toast, which
	// also reaches the client-error log with the cause attached), and the
	// await below renders a retry surface from its catch branch.
	type SetupOverlayModule = typeof import('$lib/components/setup/SetupOverlay.svelte');
	let setupOverlayModule: Promise<SetupOverlayModule> | null = null;
	function loadSetupOverlay(): Promise<SetupOverlayModule> {
		if (setupOverlayModule === null) {
			setupOverlayModule = import('$lib/components/setup/SetupOverlay.svelte');
			setupOverlayModule.catch((error: unknown) => {
				const message = error instanceof Error ? error.message : String(error);
				pushToast(`Setup failed to load: ${message}`, 'error', TOAST_DEFAULT_MS, error);
			});
		}
		return setupOverlayModule;
	}
	// The retry is a fresh document, not a second import(): the browser keeps
	// a failed module fetch in its module map, so re-importing the same URL
	// rejects again without touching the network (measured in Chromium, Thu 24
	// Sep 2026: the second import() put no request on the wire). /setup is the
	// door that reopens the wizard in the new document, and in the stale-deploy
	// case the new document also carries the new chunk URLs.
	let setupRetryAction = $state<string | null>(null);
	const setupRetryGate = runButtonActionGate(
		() => setupRetryAction,
		(value) => {
			setupRetryAction = value;
		}
	);

	function retrySetupOverlay(): void {
		void runButtonAction(setupRetryGate, 'setup-retry', () => {
			window.location.assign(SETUP_ROUTE);
		});
	}
	// The hotkeys cheatsheet, the stage view and Set notes are lazy chunks with one
	// failure shape: an error toast naming the overlay, raised again on every
	// open that fails (each loader forgets a failed attempt).
	function overlayLoadFailureReporter(overlay: string): (error: unknown) => void {
		return (error: unknown): void => {
			const message = error instanceof Error ? error.message : String(error);
			pushToast(
				`${overlay} failed to load: ${message}. Reload the page to retry.`,
				'error',
				TOAST_DEFAULT_MS,
				error
			);
		};
	}
	const reportHotkeysOverlayLoadFailure = overlayLoadFailureReporter('Hotkeys overlay');
	const reportStageOverlayLoadFailure = overlayLoadFailureReporter('Stage view');
	const reportSettingsOverlayLoadFailure = overlayLoadFailureReporter('Settings');

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
	// Issue #2653: keep the non-blocking strip up while library-attached still
	// needs setup, even after the boot gate cleared on a dismissed-empty pass.
	const showPreflightIndicator = $derived(
		shouldShowPreflightStrip(blockOnPreflight, preflightGate.cleared, preflightGate.checks)
	);

	// Keep html[data-theme] in sync (prefs module also applies on load/set).
	$effect(() => {
		if (typeof document === 'undefined') return;
		document.documentElement.dataset.theme = uiPrefs.theme;
		document.documentElement.style.colorScheme = uiPrefs.theme;
	});

	// A11Y-11811: html[data-reduce-motion] follows the OS media query OR the Appearance pref.
	$effect(() => {
		if (typeof document === 'undefined') return;
		setReduceMotionPref(uiPrefs.reduce_motion);
		applyReducedMotionAttr(document.documentElement);
		const offMedia = watchReducedMotionMedia(() => applyReducedMotionAttr(document.documentElement));
		const offPrefs = subscribeKind('ui_prefs', () => void refreshReduceMotionFromDisk());
		return () => {
			offMedia();
			offPrefs();
		};
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
		if (!isPerformance) void goto(preserveUrlSearchOnRedirect(SETUP_HOST_ROUTE, $page.url.search));
	}

	function raiseSetupOnFirstRun(): void {
		void runFirstRunGate().then((show) => {
			if (show !== true) return;
			// A probe started before dismissal can resolve after the operator
			// walked away. Honour that close instead of reopening.
			if (setupOverlay.holdEmptyReopen) return;
			openSetupForFirstRun();
		});
	}

	// Empty-library auto-open. holdEmptyReopen blocks the stale `fail` that
	// is still on screen for one poll after Skip or Start playing (#3422).
	$effect(() => {
		if (isSecondScreen) return;
		const open = shouldAutoOpenEmptyLibrarySetup(
			preflightGate.checks,
			setupOpen,
			setupOverlay.holdEmptyReopen
		);
		if (!open || finalSetupRefusal() !== null) return;
		openSetupForFirstRun();
	});

	// Poll while the incomplete note is up, same 3s cadence as the boot gate.
	$effect(() => {
		if (!setupOverlay.incomplete) return;
		void checkPreflight();
		const preflightId = setInterval(() => void checkPreflight(), 3_000);
		return () => clearInterval(preflightId);
	});

	// Run as soon as the client router is live; onMount alone is too late for
	// domcontentloaded e2e and causes a visible library flash on cold open.
	$effect(() => {
		if (typeof window === 'undefined') return;
		installBootLandingRedirect({
			goto,
			getPathname: () => $page.url.pathname,
			getSearch: () => $page.url.search,
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
		// SECONDSCREEN-11713: rootLayoutRunsMainWindowLoops gates shell polls, instruments, quit, setup.
		const mainWindow = rootLayoutRunsMainWindowLoops($page.url.pathname);
		if (mainWindow) raiseSetupOnFirstRun();
		refreshHealth();
		const uninstallSettings = installSettingsHotkeys();
		const uninstallHotkeysOverlay = installHotkeysOverlayHotkeys();
		prefetchHotkeysOverlay(reportHotkeysOverlayLoadFailure);
		prefetchStageOverlay(reportStageOverlayLoadFailure);
		prefetchSettingsOverlay(reportSettingsOverlayLoadFailure);
		const uninstallQuitGate = mainWindow ? installQuitGate() : () => {};
		// Page-lifetime instruments: usage heartbeat + the DevTools perf log
		// globals the e2e latency floor reads. See $lib/rb/app-init.
		const stopInstruments = mainWindow ? startAppInstruments() : () => {};
		const uninstallShellNavigation = mainWindow ? installShellNavigationPoll() : () => {};
		const uninstallShellCommands = mainWindow ? installShellCommandPoll() : () => {};
		let unmounted = false;
		let uninstallCommentPinHotkeys: (() => void) | null = null;
		deferFeedbackPinShell(
			(shell) => {
				if (unmounted) return;
				FeedbackPinLayer = shell.layer;
				FeedbackPinShellButton = shell.shellButton;
				FeedbackDock = shell.dock;
				uninstallCommentPinHotkeys = shell.installCommentPinHotkeys();
			},
			(error) => {
				if (unmounted) return;
				pinShellError = error instanceof Error ? error.message : String(error);
				pushToast(`Comment pins failed to load: ${pinShellError}`, 'error', TOAST_DEFAULT_MS, error);
			}
		);
		deferLoad(
			'assistant-mount:load',
			() => import('$lib/components/assistant/AgentSidebarMount.svelte'),
			(mount) => {
				if (!unmounted) AgentSidebarMount = mount.default;
			},
			(error) => {
				if (unmounted) return;
				const message = error instanceof Error ? error.message : String(error);
				pushToast(`Assistant failed to load: ${message}`, 'error', TOAST_DEFAULT_MS, error);
			},
			bootScheduler
		);
		// Set notes (SETNOTE-6340/6340b): mounts itself after boot, see set-notes/loader.ts.
		const stopSetNotes = deferSetNotesHost(overlayLoadFailureReporter('Set notes'));
		const id = setInterval(refreshHealth, 30_000);
		const uninstallHoverTooltip = installSingleHoverTooltip();
		return () => {
			uninstallHoverTooltip();
			uninstallSettings();
			uninstallHotkeysOverlay();
			uninstallQuitGate();
			stopInstruments();
			uninstallShellNavigation();
			uninstallShellCommands();
			unmounted = true;
			uninstallCommentPinHotkeys?.();
			stopSetNotes();
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
	<PreflightScreen
		mode="boot"
		blocking
		navigate={goto}
		pathname={$page.url.pathname}
		hideCheckIds={hideCheckIds}
	/>
{:else}

{#if health.bindWarning}
	<BannerWarning message={health.bindWarning} />
{/if}

{#if isFullBleedRoute}
	<!-- Nothing but the route: the way out of /performance is the top bar's
	     mode picker (Library). Library mode's Crate Desk at / is full-bleed
	     too (LIBWS-01), so its top bar's Pages menu carries the sidebar links. -->
	{@render children()}
{:else}
<div class="app-shell">
	<aside class="sidebar">
		{#if backstageOn && BackstageNav}
		<BackstageNav showDev={uiPrefs.show_dev_ui} url={$page.url} />
		{:else}
		<h1>Open DJ</h1>
		<nav>
			<!-- Links come from $lib/shell/sidebar-nav: developer pages (Queues,
			     Progress, Admin) only while "Show developer pages" is on, one
			     Settings entry, and the current page marked. -->
			{#each navLinks as link (link.href)}
				{@const current = isCurrentNavLink(link.href, $page.url.pathname)}
				{#if link.href === '/progress-tree'}
					<!-- Ledger route: legacy-daemon only, so the link says so rather than
					     leading to a page that can only apologize. -->
					<a
						href={link.href}
						class:active={current}
						class:nav-unavailable={ledgerRefusal !== null}
						aria-current={current ? 'page' : undefined}
						title={ledgerRefusal ?? 'Fan-out progress ledger (GET /api/v1/progress)'}
					>
						{link.label}
					</a>
				{:else}
					<a
						href={link.href}
						class:active={current}
						aria-current={current ? 'page' : undefined}
						title={link.title}
					>
						{link.label}
					</a>
				{/if}
			{/each}
		</nav>
		{/if}
	</aside>
	<main>
		<div class="topbar">
			<div class="status-strip" data-testid="header-status-strip">
				{#if health.data}
					<span class="readout readout-numeric" title={headerTrackCountTitle(health.data.state_db.tracks, health.data.state_db.tracks_playable ?? 0)}>{health.data.state_db.tracks} tracks ({health.data.state_db.tracks_playable ?? 0} playable)</span>
					<span class="sep" aria-hidden="true">{' · '}</span>
					<span class="readout readout-numeric" title={headerPlaylistCountTitle(health.data.state_db.playlists)}>{health.data.state_db.playlists} playlists</span>
					<span class="sep" aria-hidden="true">{' · '}</span>
					{#if health.data.cloud.lock_holder}
						<span class="readout">lock: {health.data.cloud.lock_holder.holder}</span>
					{:else}
						<span class="readout">lock: free</span>
					{/if}
					{#if health.data.syncthing}
						<span class="sep" aria-hidden="true">{' · '}</span>
						<span class="readout">syncthing: {health.data.syncthing.peers_connected} peers - {health.data.syncthing.folder_state}</span>
					{/if}
					<span class="sep" aria-hidden="true">{' · '}</span>
					<span class="readout">bind: {health.data.bind_host}</span>
				{:else}
					<span class="readout">Connecting to configured worktree daemon...</span>
				{/if}
			</div>
			<!-- SETNOTE-6340g: upgraded by the set-notes app once it loads (lib/set-notes/toggle-element.ts). -->
			<set-notes-toggle></set-notes-toggle>
			<CloudSyncStatusChip />
			<FeedbackPinTopbarControls />
			{#if FeedbackPinShellButton}
				<FeedbackPinShellButton />
			{:else if pinShellError}
				<span
					class="fb-shell-pin-slot fb-shell-pin-failed"
					role="img"
					aria-label="Comment pins failed to load"
					title={`Comment pins failed to load: ${pinShellError}. Reload the page to retry.`}>!</span
				>
			{:else}
				<!-- Holds the button's 28px so the topbar does not shift when it arrives. -->
				<span class="fb-shell-pin-slot" aria-hidden="true"></span>
			{/if}
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
	<PreflightScreen
		mode="boot"
		blocking={false}
		navigate={goto}
		pathname={$page.url.pathname}
		hideCheckIds={hideCheckIds}
	/>
{/if}

<!-- Settings: mounted at the root so it works on /performance too, but only
     while open, from a lazy chunk (see settings-overlay-loader.ts). Opening it
     yields the blocking boot gate at once, so while a cold chunk is in flight
     the setup backdrop covers the app in its place (Sol + Codex P2, PR #6829). -->
{#if isSettingsOverlayOpen()}
	{#await loadSettingsOverlay(reportSettingsOverlayLoadFailure)}
		<div class="setup-load-backdrop" role="presentation">
			<div class="setup-load-pending" role="status" aria-label="Settings is loading">Loading settings&hellip;</div>
		</div>
	{:then { default: SettingsOverlay }}
		<SettingsOverlay />
	{:catch}
		<!-- Already reported as an error toast by reportSettingsOverlayLoadFailure. -->
	{/await}
{/if}
<!-- STAGE lyric view: mounted at the root so it survives navigation, but only
     while open, from a lazy chunk (see stage-overlay-loader.ts). -->
{#if isStageOverlayOpen()}
	{#await loadStageOverlay(reportStageOverlayLoadFailure) then { default: StageOverlay }}
		<StageOverlay />
	{:catch}
		<!-- Already reported as an error toast by reportStageOverlayLoadFailure. -->
	{/await}
{/if}
<!-- The first-run wizard, over whatever route is on screen. Mounted at the
     root for the same reason SettingsOverlay is: /performance bypasses the app
     shell, and the one surface a brand new user meets cannot be missing there
     of all places. Imported lazily (the payback named in
     scripts/bundle-budget.mjs): the wizard renders only while the overlay is
     open or has just been dismissed with an empty library (the incomplete
     note), and its own effects early-return while closed, so keeping it off
     the first paint changes nothing a user or an agent can observe while it
     is closed. While it is OPEN and the chunk is still in flight, the
     backdrop below covers the app: `setupOpen` has already yielded the boot
     gate, so without it a fresh install could use the app for the length of
     the download (Codex review of #3862 at 079105b3, P2). -->
{#if setupMounted}
	{#await loadSetupOverlay()}
		<div class="setup-load-backdrop" role="presentation">
			<div class="setup-load-pending" role="status" aria-label="Setup is loading">Loading setup&hellip;</div>
		</div>
	{:then { default: SetupOverlay }}
		<SetupOverlay />
	{:catch error}
		<!-- The chunk did not arrive. Same backdrop the wizard uses, so the ask
		     still sits over the app rather than vanishing, and one action that
		     fetches the chunk again (see retrySetupOverlay). -->
		<div class="setup-load-backdrop" role="presentation">
			<div class="setup-load-failed" role="alertdialog" aria-label="Setup failed to load">
				<p class="setup-load-failed-reason">
					Setup failed to load: {error instanceof Error ? error.message : String(error)}
				</p>
				<button
					type="button"
					class="setup-load-retry"
					onclick={retrySetupOverlay}
					disabled={isRunButtonActionPending(setupRetryGate)}
					aria-busy={isRunButtonActionPending(setupRetryGate, 'setup-retry')}
				>
					Reload and retry
				</button>
			</div>
		</div>
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
{#if isHotkeysOverlayOpen()}
	{#await loadHotkeysOverlay(reportHotkeysOverlayLoadFailure) then { default: HotkeysOverlay }}
		<HotkeysOverlay />
	{:catch}
		<!-- Already reported as an error toast by reportHotkeysOverlayLoadFailure. -->
	{/await}
{/if}
<QuitConfirmOverlay />
<!-- At the root so every route, full-bleed or not, gets the v2 Cmd K palette (#9467). -->
{#if CommandPaletteHost && cmdkV2Enabled() && !blockOnPreflight}
	<CommandPaletteHost />
{/if}
<!-- The assistant (Cmd+I, AISIDE-7892j): at the root so full-bleed routes get it
     too. Imported by a deferred boot task, so it and its flag read cost first
     paint nothing; the mount shows nothing unless `assistant.sidebar` is on. -->
{#if AgentSidebarMount}
	<AgentSidebarMount />
{/if}
<!-- The diagnostics consent dialog (OBS-05) is mounted by $lib/telemetry-consent
     from a deferred boot task, so neither it nor its module is on the
     first-paint path or in the library page's bundle budget. -->

<ToastStack items={visibleToasts} />
<div
	class="a11y-announcer-sr-only"
	aria-live="polite"
	aria-atomic="true"
	data-testid="a11y-announcer"
	use:announcer
></div>
{#if FeedbackPinLayer}
	<FeedbackPinLayer />
{/if}
{#if FeedbackDock}
	<FeedbackDock />
{/if}
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
	/* The page on screen: aria-current="page" plus a visible accent bar and
	   color, on top of app.css's shared .active background. */
	.sidebar nav a[aria-current='page'] {
		color: var(--accent);
		font-weight: 600;
		box-shadow: inset 3px 0 0 var(--accent);
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
	/* Same box as FeedbackPinShellButton's .fb-shell-pin, empty until it loads. */
	.fb-shell-pin-slot {
		flex: none;
		width: 28px;
		height: 28px;
	}
	.fb-shell-pin-failed {
		display: grid;
		place-items: center;
		color: var(--danger);
		font-weight: 700;
		cursor: help;
	}
	/* Same box and layer as SetupOverlay's .su-backdrop / .su-panel, minus the
	   wizard: what the user sees when the wizard's chunk could not be fetched. */
	.setup-load-backdrop {
		position: fixed;
		inset: 0;
		z-index: 380;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 3vh 1rem;
		background: rgba(0, 0, 0, 0.55);
	}
	.setup-load-pending {
		padding: 1rem 1.5rem;
		border: 1px solid var(--border, #2a3140);
		border-radius: 8px;
		background: var(--surface, #121720);
		color: var(--muted, #9aa4b2);
	}
	.setup-load-failed {
		display: grid;
		gap: 0.75rem;
		max-width: 32rem;
		padding: 1.25rem 1.5rem;
		border: 1px solid var(--danger);
		border-radius: 8px;
		background: var(--surface, #121720);
	}
	.setup-load-failed-reason {
		margin: 0;
		color: var(--danger);
	}
	.setup-load-retry {
		justify-self: start;
	}
	/* SETNOTE-6340g: holds the toggle's size so the bar does not shift when it is upgraded. */
	set-notes-toggle {
		display: inline-flex;
		flex: 0 0 22px;
		height: 20px;
	}
	.a11y-announcer-sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}
</style>
