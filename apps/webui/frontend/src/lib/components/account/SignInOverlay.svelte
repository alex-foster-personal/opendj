<script lang="ts">
	/**
	 * Full-screen blocker for Google sign-in. Shown for the whole attempt:
	 * briefly on the in-page redirect path, and for the full desktop-shell
	 * system-browser poll.
	 */
	import {
		cancelSignIn,
		signInOverlay,
		signInStatusText
	} from '$lib/auth/sign-in-overlay.svelte';
	import SignInOverlayView from './SignInOverlayView.svelte';
</script>

<SignInOverlayView
	open={signInOverlay.open}
	phase={signInOverlay.phase}
	statusText={signInStatusText(signInOverlay.phase)}
	onCancel={() => cancelSignIn()}
/>
