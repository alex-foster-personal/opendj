import { accountOverlay } from '$lib/account/overlay.svelte';
import { signInOverlay } from '$lib/auth/sign-in-overlay.svelte';
import { isHotkeysOverlayOpen } from '$lib/components/rb/hotkeys/hotkeys-overlay.svelte';
import { isSettingsOpen } from '$lib/settings/overlay.svelte';
import { isSetupOverlayOpen } from '$lib/setup/overlay.svelte';

/** True when any root-layout modal overlay is open (settings, setup, account, etc.). */
export function anyRootOverlayOpen(): boolean {
	return (
		isSettingsOpen() ||
		isSetupOverlayOpen() ||
		isHotkeysOverlayOpen() ||
		accountOverlay.open ||
		signInOverlay.open
	);
}
