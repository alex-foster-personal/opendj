<!--
  Circular account bauble for the top-right of the app shell.

  Signed out: a plain circle with a generic avatar outline. Clicking it
  starts Google sign-in and navigates to the consent screen.

  Signed in: the Google account picture, falling back to the first letter of
  the name or email when the picture fails to load or Google supplied none.
  Clicking opens a small menu with the account email and Sign out.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { auth, logout, refreshUser, startLogin } from '$lib/auth.svelte';
	import { openAccountOverlay } from '$lib/account/overlay.svelte';
	import { bootScheduler } from '$lib/rb/boot-scheduler';
	import { pushToast } from '$lib/stores.svelte';

	// The performance route's topbar is ~28px tall, so the shell's 32px bauble
	// does not fit there. Size is a prop rather than a CSS override so the
	// glyph and initial scale with the circle.
	let { size = 32 }: { size?: number } = $props();

	let menuOpen = $state(false);
	let busy = $state(false);
	/** Set when the avatar URL 404s or Google returns no picture. */
	let avatarBroken = $state(false);

	const initial = $derived(
		(auth.user?.name?.trim()?.[0] ?? auth.user?.email?.[0] ?? '?').toUpperCase()
	);
	const showAvatar = $derived(Boolean(auth.user?.avatar_url) && !avatarBroken);
	const label = $derived(
		auth.user ? `Signed in as ${auth.user.email}` : 'Sign in with Google'
	);

	onMount(() => {
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

	async function onBaubleClick(): Promise<void> {
		if (auth.user) {
			menuOpen = !menuOpen;
			return;
		}
		busy = true;
		try {
			const { authorization_url } = await startLogin();
			window.location.href = authorization_url;
		} catch (exc) {
			// Most likely cause is a daemon with no OAuth client configured;
			// its 503 message is the provisioning runbook, so show it rather
			// than a generic "sign-in failed".
			pushToast(exc instanceof Error ? exc.message : 'sign-in failed', 'error', 12000);
			busy = false;
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
		busy = true;
		try {
			await logout();
			menuOpen = false;
		} catch (exc) {
			pushToast(exc instanceof Error ? exc.message : 'sign-out failed', 'error');
		} finally {
			busy = false;
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
		disabled={busy || auth.loading}
		aria-haspopup={auth.user ? 'menu' : undefined}
		aria-expanded={auth.user ? menuOpen : undefined}
		aria-label={label}
		title={auth.loading ? 'Checking sign-in status...' : label}
		onclick={onBaubleClick}
	>
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
	</button>

	{#if menuOpen && auth.user}
		<div class="menu" role="menu">
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
	.bauble:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.bauble:disabled {
		cursor: progress;
		opacity: 0.6;
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
		position: absolute;
		top: calc(100% + 6px);
		right: 0;
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
