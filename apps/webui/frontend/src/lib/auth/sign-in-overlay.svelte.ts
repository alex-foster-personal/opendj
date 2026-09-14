/**
 * Full-screen blocking state for Google sign-in.
 *
 * Mounted once from the root layout (SignInOverlay.svelte), same pattern as
 * AccountOverlay and SetupOverlay. UserBauble and the overlay read the same
 * `phase` so "busy" cannot drift between the bauble and the blocker.
 */

export type SignInPhase =
	| 'idle'
	| 'starting'
	| 'opening-browser'
	| 'waiting'
	| 'redirecting';

let phase = $state<SignInPhase>('idle');
let activeCancel: (() => void) | null = null;

export function signInStatusText(current: SignInPhase): string {
	switch (current) {
		case 'starting':
			return 'Starting sign-in...';
		case 'opening-browser':
			return 'Opening your browser...';
		case 'waiting':
			return 'Waiting for Google sign-in...';
		case 'redirecting':
			return 'Signing you in...';
		default:
			return '';
	}
}

/** THE door in. Raises the overlay before startLogin() runs. */
export function beginSignIn(): void {
	phase = 'starting';
}

export function setSignInPhase(next: SignInPhase): void {
	phase = next;
}

/** Wire the desktop-shell poll loop's cancel into the overlay Cancel button. */
export function registerSignInCancel(cancel: () => void): void {
	activeCancel = cancel;
}

/** User pressed Cancel while waiting for the system browser. */
export function cancelSignIn(): void {
	activeCancel?.();
	activeCancel = null;
	phase = 'idle';
}

/** Sign-in finished successfully. */
export function completeSignIn(): void {
	activeCancel = null;
	phase = 'idle';
}

/** Sign-in failed, timed out, or was aborted programmatically. */
export function failSignIn(): void {
	activeCancel?.();
	activeCancel = null;
	phase = 'idle';
}

export function _resetSignInOverlayForTests(): void {
	activeCancel = null;
	phase = 'idle';
}

export const signInOverlay = {
	get open() {
		return phase !== 'idle';
	},
	get phase() {
		return phase;
	},
	get busy() {
		return phase !== 'idle';
	}
};
