<script lang="ts">
	// Account button plus the explainer around the sign-in bauble, split out
	// of TopBar so the topbar's import fan-out stays within the quality
	// ratchet. TopBar still renders the bauble itself as the children.
	import type { Snippet } from 'svelte';
	import ControlExplainer from '$lib/components/rb/deck/ControlExplainer.svelte';
	import { auth } from '$lib/auth.svelte';
	import { openAccountOverlay } from '$lib/account/overlay.svelte';

	let { children }: { children: Snippet } = $props();

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
<ControlExplainer
	title={auth.user ? 'Signed-in features' : 'Sign in required'}
	bullets={loginGatedBullets}
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
