<script lang="ts">
	// The explainer around the sign-in bauble, split out
	// of TopBar so the topbar's import fan-out stays within the quality
	// ratchet. TopBar still renders the bauble itself as the children.
	import type { Snippet } from 'svelte';
	import ControlExplainer from '$lib/components/rb/deck/ControlExplainer.svelte';
	import { auth } from '$lib/auth.svelte';

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

<!-- ACCT-01 / LESSV-04 (the maintainer, Tue 6 Oct 2026): the account button lives ONLY
     inside the login dropdown (UserBauble's menu, "Account"), in both views.
     The standalone pill that used to sit here duplicated it. The overlay and
     its route are unchanged; only this second door is gone. -->
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
	.login-cluster.signed-in {
		color: var(--rb-green);
	}
</style>
