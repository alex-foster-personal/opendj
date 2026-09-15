/**
 * Desktop-shell Google sign-in: open the consent URL in the OS browser and
 * poll /api/v1/auth/me until the session cookie lands.
 *
 * Plain functions over injectable deps so unit tests can drive success,
 * cancel, and timeout without real timers or network.
 */

import type { AuthUser } from '../auth.svelte';
import type { ShellScope } from '../rb/usage-heartbeat';

export const POLL_INTERVAL_MS = 1500;
export const SIGN_IN_TIMEOUT_MS = 3 * 60 * 1000;

export type SignInPollResult = 'success' | 'cancelled' | 'timeout';

export type UsageSurface = 'desktop-shell' | 'browser';

export interface DesktopSignInDeps {
	refreshUser: () => Promise<void>;
	getUser: () => AuthUser | null;
	setInterval: typeof globalThis.setInterval;
	clearInterval: typeof globalThis.clearInterval;
	setTimeout: typeof globalThis.setTimeout;
	clearTimeout: typeof globalThis.clearTimeout;
}

export interface SignInNavigationDeps extends DesktopSignInDeps {
	detectSurface: (scope: Window & ShellScope) => UsageSurface;
	openUrl: (url: string) => Promise<void>;
	assignLocation: (url: string) => void;
}

export interface DesktopSignInSession {
	cancel: () => void;
	done: Promise<SignInPollResult>;
}

export function startDesktopShellSignInPoll(deps: DesktopSignInDeps): DesktopSignInSession {
	let settled = false;
	let intervalId: ReturnType<typeof globalThis.setInterval> | null = null;
	let timeoutId: ReturnType<typeof globalThis.setTimeout> | null = null;
	let resolveDone: (result: SignInPollResult) => void;

	const done = new Promise<SignInPollResult>((resolve) => {
		resolveDone = resolve;
	});

	const cleanup = (): void => {
		if (intervalId !== null) deps.clearInterval(intervalId);
		if (timeoutId !== null) deps.clearTimeout(timeoutId);
		intervalId = null;
		timeoutId = null;
	};

	const finish = (result: SignInPollResult): void => {
		if (settled) return;
		settled = true;
		cleanup();
		resolveDone(result);
	};

	const poll = async (): Promise<void> => {
		if (settled) return;
		await deps.refreshUser();
		if (settled) return;
		if (deps.getUser() !== null) finish('success');
	};

	intervalId = deps.setInterval(() => void poll(), POLL_INTERVAL_MS);
	timeoutId = deps.setTimeout(() => finish('timeout'), SIGN_IN_TIMEOUT_MS);

	return {
		cancel() {
			finish('cancelled');
		},
		done
	};
}

export type SignInNavigationResult =
	| { surface: 'browser' }
	| { surface: 'desktop-shell'; session: DesktopSignInSession };

export interface SignInNavigationHooks {
	beforeOpenUrl?: () => void;
	afterOpenUrl?: () => void;
}

/**
 * After startLogin() returns, either redirect in-page (browser) or open the
 * system browser and start polling (desktop shell).
 */
export async function navigateForSignIn(
	authorizationUrl: string,
	window: Window & ShellScope,
	deps: SignInNavigationDeps,
	hooks: SignInNavigationHooks = {}
): Promise<SignInNavigationResult> {
	if (deps.detectSurface(window) === 'browser') {
		deps.assignLocation(authorizationUrl);
		return { surface: 'browser' };
	}
	hooks.beforeOpenUrl?.();
	await deps.openUrl(authorizationUrl);
	hooks.afterOpenUrl?.();
	return { surface: 'desktop-shell', session: startDesktopShellSignInPoll(deps) };
}
