/**
 * Orchestrates Google sign-in after /api/v1/auth/login returns the consent URL.
 */

import { detectSurface, type ShellScope } from '$lib/rb/usage-heartbeat';
import { openExternal } from '$lib/shell/native-shell';
import { auth, refreshUser } from '$lib/auth.svelte';
import { pushToast } from '$lib/stores.svelte';
import {
	completeSignIn,
	failSignIn,
	registerSignInCancel,
	setSignInPhase
} from '$lib/auth/sign-in-overlay.svelte';
import {
	navigateForSignIn,
	type SignInNavigationDeps
} from '$lib/auth/system-browser-signin';

function defaultNavigationDeps(): SignInNavigationDeps {
	return {
		detectSurface,
		openUrl: (url) => openExternal(url),
		assignLocation: (url) => {
			window.location.href = url;
		},
		refreshUser,
		getUser: () => auth.user,
		setInterval: globalThis.setInterval.bind(globalThis),
		clearInterval: globalThis.clearInterval.bind(globalThis),
		setTimeout: globalThis.setTimeout.bind(globalThis),
		clearTimeout: globalThis.clearTimeout.bind(globalThis)
	};
}

/**
 * Complete sign-in once the consent URL is known. Caller must already have
 * called beginSignIn() so the blocking overlay is visible.
 */
export async function runSignInAfterStartLogin(
	authorizationUrl: string,
	deps: Partial<SignInNavigationDeps> = {}
): Promise<void> {
	const resolved = { ...defaultNavigationDeps(), ...deps };
	const scope = window as Window & ShellScope;
	if (resolved.detectSurface(scope) === 'browser') {
		setSignInPhase('redirecting');
	}
	const navigation = await navigateForSignIn(authorizationUrl, scope, resolved, {
		beforeOpenUrl: () => setSignInPhase('opening-browser'),
		afterOpenUrl: () => setSignInPhase('waiting')
	});

	if (navigation.surface === 'browser') {
		return;
	}

	registerSignInCancel(() => navigation.session.cancel());

	const result = await navigation.session.done;
	if (result === 'success') {
		completeSignIn();
		return;
	}
	if (result === 'cancelled') {
		return;
	}
	pushToast(
		'Google sign-in timed out. Click the account button to try again.',
		'error',
		12000
	);
	failSignIn();
}
