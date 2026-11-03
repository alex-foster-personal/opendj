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
	import { onMount } from 'svelte';
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
	import UserBaubleView from './UserBaubleView.svelte';

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

	/** Sign-out only; sign-in busy comes from signInOverlay so the bauble and blocker stay in sync. */
	let signOutBusy = $state(false);
	const busy = $derived(signOutBusy || signInOverlay.busy);
	/** The daemon answered that it has no OAuth client, so sign-in cannot
	 * succeed. Stays false while the bit is UNKNOWN (null): "not answered yet"
	 * is not the same claim as "not configured". */
	const signInUnavailable = $derived(
		capabilities.googleOAuthConfigured === false && auth.user === null
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
</script>

<UserBaubleView
	{size}
	{showLabel}
	bind:menuOpen
	{auth}
	{busy}
	{signInUnavailable}
	{onBaubleClick}
	{onOpenAccount}
	{onSignOut}
/>
