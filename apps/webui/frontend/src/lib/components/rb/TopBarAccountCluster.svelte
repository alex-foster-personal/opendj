<script lang="ts">
	// Account button plus the explainer around the sign-in bauble, split out
	// of TopBar so the topbar's import fan-out stays within the quality
	// ratchet. TopBar still renders the bauble itself as the children.
	import type { Snippet } from 'svelte';
	import ControlExplainer from '$lib/components/rb/deck/ControlExplainer.svelte';
	import { auth } from '$lib/auth.svelte';
	import { openAccountOverlay } from '$lib/account/overlay.svelte';

	// `signedIn` is relayed to the parent as a bindable so TopBar can derive
	// its own showClock (CHROME-06) without importing `auth` itself, which
	// keeps TopBar's own import fan-out inside the quality ratchet (this
	// file already exists for exactly that reason, see the note above).
	// `menuOpen` is the bauble's own account menu, relayed by TopBar. That menu
	// is a descendant of the explainer below, so moving onto it never dismisses
	// the explainer, which then paints over Sign out (z-index 80 over 40).
	let {
		children,
		signedIn = $bindable(false),
		menuOpen = false
	}: { children: Snippet; signedIn?: boolean; menuOpen?: boolean } = $props();

	$effect(() => {
		signedIn = auth.user !== null;
	});

	const loginGatedBullets = [
		'Google sign-in for account panel and CloudSync fleet adopt',
		'Feedback pin sync across devices',
		'CloudSync enrollment and remote library features'
	];
</script>

{#if auth.user}
	<button
		type="button"
		class="account-btn"
		aria-label="Open account panel"
		title="Open account panel"
		onclick={() => openAccountOverlay()}
	>
		Account
	</button>
{/if}
<!-- No bullets while the menu is open: the explainer has nothing rich to show, so it stays hidden. -->
<ControlExplainer
	title={auth.user ? 'Signed-in features' : 'Sign in required'}
	bullets={menuOpen ? [] : loginGatedBullets}
	showDelayMs={60}
>
	<span class="login-cluster" class:signed-in={auth.user !== null}>
		{@render children()}
	</span>
</ControlExplainer>

<style>
	.account-btn {
		background: transparent;
		border: 1px solid var(--rb-border);
		border-radius: 999px;
		color: var(--rb-text-dim);
		font-size: 10px;
		padding: 2px 8px;
		cursor: pointer;
	}
	.account-btn:hover {
		color: var(--rb-text);
		border-color: var(--rb-text-dim);
	}
	.login-cluster.signed-in {
		color: var(--rb-green);
	}
</style>
