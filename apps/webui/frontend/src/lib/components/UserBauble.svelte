<!--
  Circular account bauble for the top-right of the app shell.

  Sign-in is per browser profile: the session is an httpOnly cookie stored
  only in this profile's jar, so another Chrome profile or Safari stays
  signed out until the operator signs in there too.

  Signed out: a plain circle with a generic avatar outline. Clicking it
  starts Google sign-in and navigates to the consent screen.

  Signed in: the Google account picture, falling back to the first letter of
  the name or email when the picture fails to load or Google supplied none.
  Clicking opens a small menu with the account email and Sign out.

  The circle is the whole control in the app shell, where the surrounding
  chrome already says what the app is. On /performance it is the ONLY sign-in
  affordance on the route, so that caller asks for `showLabel` and gets a
  labelled pill instead - a bare circle with the words in aria-label only
  reads as decoration (issue #2357).

  When the daemon reports `google_oauth_configured` false the control is
  DISABLED and says so. Offering an action the daemon cannot perform makes the
  user spend a click to learn what the page already knew.
-->
<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { clampToViewport } from '$lib/ui/clamp-to-viewport';
	import {
		auth,
		consumeAuthErrorFromLocation,
		logout,
		refreshUser,
		startLogin
	} from '$lib/auth.svelte';
	import { beginSignIn, failSignIn, signInOverlay } from '$lib/auth/sign-in-overlay.svelte';
	import { runSignInAfterStartLogin } from '$lib/auth/sign-in-flow';
	import { capabilities } from '$lib/api/capabilities.svelte';
	import { openAccountOverlay } from '$lib/account/overlay.svelte';
	import { bootScheduler } from '$lib/rb/boot-scheduler';
	import { pushToast } from '$lib/stores.svelte';

	/** Why the control is dead, and what turns it back on. Names the env pair
	 * `apps/webui/server/auth.py` reads, because the alternative is a user
	 * clicking a button that answers 503 with the same sentence. */
	const UNAVAILABLE_LABEL =
		'Google sign-in is unavailable: this daemon has no OAuth client configured. ' +
		'Set OPENDJ_GOOGLE_OAUTH_CLIENT_ID and OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET on the daemon.';

	// The performance route's topbar is ~28px tall, so the shell's 32px bauble
	// does not fit there. Size is a prop rather than a CSS override so the
	// glyph and initial scale with the circle.
	// `menuOpen` is bindable so the top bar's account explainer can stand down
	// while this menu is open (it would otherwise paint over Sign out).
	let {
		size = 32,
		showLabel = false,
		menuOpen = $bindable(false)
	}: { size?: number; showLabel?: boolean; menuOpen?: boolean } = $props();

	let menuEl = $state<HTMLDivElement | null>(null);
	let menuStyle = $state('');
	/** Sign-out only; sign-in busy comes from signInOverlay so the bauble and blocker stay in sync. */
	let signOutBusy = $state(false);
	const busy = $derived(signOutBusy || signInOverlay.busy);
	/** Set when the avatar URL 404s or Google returns no picture. */
	let avatarBroken = $state(false);

	const initial = $derived(
		(auth.user?.name?.trim()?.[0] ?? auth.user?.email?.[0] ?? '?').toUpperCase()
	);
	const showAvatar = $derived(Boolean(auth.user?.avatar_url) && !avatarBroken);

	/** The daemon answered that it has no OAuth client, so sign-in cannot
	 * succeed. Stays false while the bit is UNKNOWN (null): "not answered yet"
	 * is not the same claim as "not configured". */
	const signInUnavailable = $derived(
		capabilities.googleOAuthConfigured === false && auth.user === null
	);

	/** The control's accessible name. */
	const label = $derived(
		auth.user
			? `Signed in as ${auth.user.email}`
			: signInUnavailable
				? UNAVAILABLE_LABEL
				: 'Sign in with Google'
	);

	/** The same claim in the short form a 28px top bar can carry. One string
	 * per state, read by both the label and the title, so what is on screen
	 * and what is announced cannot drift apart. */
	const visibleLabel = $derived(
		auth.user
			? (auth.user.name?.trim() || auth.user.email)
			: signInUnavailable
				? 'Sign-in unavailable'
				: 'Sign in with Google'
	);

	onMount(() => {
		const callbackError = consumeAuthErrorFromLocation();
		if (callbackError) {
			pushToast(callbackError, 'error', 12000);
		}
		// Deferred out of the boot burst (PERF-R6). The bauble renders its
		// signed-out face while the answer is outstanding, which is what it
		// already did for the duration of the request; nobody is waiting on
		// it at second zero, and the deck load is.
		bootScheduler.defer('user-bauble:refreshUser', () => {
			void refreshUser();
		});
	});

	// A fresh sign-in means a fresh picture URL; let it try to load again.
	$effect(() => {
		if (auth.user?.avatar_url) avatarBroken = false;
	});

	async function placeMenu(): Promise<void> {
		if (!menuOpen || menuEl === null) return;
		const trigger = document.querySelector('.bauble-root .bauble');
		if (!(trigger instanceof HTMLElement)) return;
		await tick();
		const triggerRect = trigger.getBoundingClientRect();
		const menuRect = menuEl.getBoundingClientRect();
		const box = clampToViewport(
			triggerRect.right - menuRect.width,
			triggerRect.bottom + 6,
			{ width: menuRect.width, height: menuRect.height },
			{ width: window.innerWidth, height: window.innerHeight }
		);
		menuStyle = `left:${Math.round(box.x)}px;top:${Math.round(box.y)}px`;
	}

	$effect(() => {
		if (!menuOpen) return;
		void placeMenu();
	});

	async function onBaubleClick(): Promise<void> {
		if (auth.user) {
			menuOpen = !menuOpen;
			return;
		}
		beginSignIn();
		try {
			const { authorization_url } = await startLogin();
			await runSignInAfterStartLogin(authorization_url);
		} catch (exc) {
			// Most likely cause is a daemon with no OAuth client configured;
			// its 503 message is the provisioning runbook, so show it rather
			// than a generic "sign-in failed".
			failSignIn();
			pushToast(exc instanceof Error ? exc.message : 'sign-in failed', 'error', 12000);
		}
	}

	/** THE door into the account panel (ACCT-01). This menu used to offer only
	 * "Sign out", which left identity, plan and everything stored locally
	 * reachable by terminal only. */
	function onOpenAccount(): void {
		menuOpen = false;
		openAccountOverlay();
	}

	async function onSignOut(): Promise<void> {
		signOutBusy = true;
		try {
			await logout();
			menuOpen = false;
		} catch (exc) {
			pushToast(exc instanceof Error ? exc.message : 'sign-out failed', 'error');
		} finally {
			signOutBusy = false;
		}
	}

	function onWindowClick(event: MouseEvent): void {
		if (!menuOpen) return;
		const target = event.target as HTMLElement | null;
		if (target?.closest('.bauble-root')) return;
		menuOpen = false;
	}
</script>

<svelte:window
	onclick={onWindowClick}
	onkeydown={(e) => {
		if (e.key === 'Escape') menuOpen = false;
	}}
/>

<div class="bauble-root" style={`--bauble-size: ${size}px`}>
	<button
		type="button"
		class="bauble"
		class:signed-in={Boolean(auth.user)}
		class:pilled={showLabel}
		class:unavailable={signInUnavailable}
		disabled={busy || signInUnavailable || (Boolean(auth.user) && auth.loading)}
		aria-haspopup={auth.user ? 'menu' : undefined}
		aria-expanded={auth.user ? menuOpen : undefined}
		aria-label={label}
		title={signInUnavailable
			? UNAVAILABLE_LABEL
			: auth.loading
				? 'Checking sign-in status...'
				: label}
		onclick={onBaubleClick}
	>
		<span class="bauble-face">
			{#if showAvatar && auth.user}
				<img
					src={auth.user.avatar_url}
					alt=""
					referrerpolicy="no-referrer"
					onerror={() => (avatarBroken = true)}
				/>
			{:else if auth.user}
				<span class="initial">{initial}</span>
			{:else}
				<!-- Generic avatar outline: head + shoulders, no fill. -->
				<svg viewBox="0 0 24 24" aria-hidden="true">
					<circle cx="12" cy="9" r="3.4" />
					<path d="M5.6 19.2a6.7 6.7 0 0 1 12.8 0" />
				</svg>
			{/if}
		</span>
		{#if showLabel}
			<span class="bauble-label">{visibleLabel}</span>
		{/if}
	</button>

	{#if menuOpen && auth.user}
		<div class="menu" bind:this={menuEl} style={menuStyle} role="menu">
			<div class="menu-account">
				{#if auth.user.name}
					<span class="menu-name">{auth.user.name}</span>
				{/if}
				<span class="menu-email" title="The Google account this webui is signed in as"
					>{auth.user.email}</span
				>
			</div>
			<button
				type="button"
				class="menu-item"
				role="menuitem"
				onclick={onOpenAccount}
				title="Open the account panel: your identity, your plan, and exactly what is stored about you on this machine."
			>
				Account
			</button>
			<button type="button" class="menu-item" role="menuitem" disabled={busy} onclick={onSignOut}>
				Sign out
			</button>
		</div>
	{/if}
</div>

<style>
	.bauble-root {
		position: relative;
		display: inline-flex;
		margin-left: auto;
	}
	.bauble {
		width: var(--bauble-size, 32px);
		height: var(--bauble-size, 32px);
		padding: 0;
		/* --border on --surface is near-black on near-black: the circle reads as
		   invisible in the topbar and only the glyph shows. --muted is the
		   dimmest token that still resolves as a ring against the topbar. */
		border: 1px solid var(--muted);
		border-radius: 50%;
		background: var(--bg);
		color: var(--muted);
		display: grid;
		place-items: center;
		overflow: hidden;
		cursor: pointer;
		transition: border-color 120ms ease, color 120ms ease, background 120ms ease;
	}
	/* Labeled pill: the circle plus its text, as ONE hit target, so a click on
	   the words is a click on the control. Height stays the callers' size. */
	.bauble.pilled {
		width: auto;
		max-width: 100%;
		min-width: 0;
		padding-right: 8px;
		border-radius: 999px;
		display: inline-flex;
		align-items: center;
		gap: 5px;
	}
	.bauble-face {
		width: var(--bauble-size, 32px);
		height: var(--bauble-size, 32px);
		flex: 0 0 auto;
		display: grid;
		place-items: center;
		overflow: hidden;
	}
	.bauble-label {
		font-family: var(--rb-font, inherit);
		font-size: 10px;
		line-height: 1;
		min-width: 0;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.bauble:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.bauble:disabled {
		cursor: progress;
		opacity: 0.6;
	}
	/* Unavailable is not busy: nothing is in flight and nothing will be. */
	.bauble.unavailable,
	.bauble.unavailable:disabled {
		cursor: not-allowed;
		opacity: 1;
		border-color: var(--border);
		border-style: dashed;
		color: var(--muted);
	}
	.bauble.signed-in {
		border-color: var(--accent-dim);
	}
	.bauble img {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
	}
	.bauble svg {
		width: calc(var(--bauble-size, 32px) * 0.55);
		height: calc(var(--bauble-size, 32px) * 0.55);
		fill: none;
		stroke: currentColor;
		stroke-width: 1.6;
		stroke-linecap: round;
	}
	.initial {
		font-size: calc(var(--bauble-size, 32px) * 0.42);
		font-weight: 600;
		color: var(--accent);
	}
	.menu {
		position: fixed;
		min-width: 200px;
		padding: 0.4rem;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--surface);
		box-shadow: 0 6px 20px rgb(0 0 0 / 45%);
		z-index: 40;
	}
	.menu-account {
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
		padding: 0.3rem 0.45rem 0.45rem;
		border-bottom: 1px solid var(--border);
	}
	.menu-name {
		font-size: 0.78rem;
		color: var(--fg);
	}
	.menu-email {
		font-size: 0.72rem;
		color: var(--muted);
		word-break: break-all;
	}
	.menu-item {
		width: 100%;
		margin-top: 0.35rem;
		padding: 0.35rem 0.45rem;
		border: none;
		border-radius: 4px;
		background: transparent;
		color: var(--accent);
		text-align: left;
		font: inherit;
		font-size: 0.78rem;
		cursor: pointer;
	}
	.menu-item:hover:not(:disabled) {
		background: var(--bg);
	}
	.menu-item:disabled {
		cursor: progress;
		opacity: 0.6;
	}
</style>
