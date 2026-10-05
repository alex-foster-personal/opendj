/**
 * PERFMODE-11: one-shot cold-open redirect from `/` to the boot landing route.
 * Session-gated so Library navigation after first open (the mode picker) is untouched.
 */
import { resolveBootLandingRoute } from './app-mode-landing';

export const BOOT_LANDING_SESSION_KEY = 'mdt.boot-landing.applied.v1';

export function shouldApplyBootLanding(pathname: string, bootApplied: boolean): boolean {
	return !bootApplied && pathname === '/';
}

export function markBootLandingApplied(): void {
	try {
		sessionStorage.setItem(BOOT_LANDING_SESSION_KEY, '1');
	} catch {
		/* quota / private mode */
	}
}

export function isBootLandingApplied(): boolean {
	try {
		return sessionStorage.getItem(BOOT_LANDING_SESSION_KEY) !== null;
	} catch {
		return true;
	}
}

export interface BootLandingDeps {
	goto: (route: string, opts?: { replaceState?: boolean }) => void | Promise<void>;
	getPathname: () => string;
	readBootStamp: () => string | null;
	nowMs?: () => number;
}

export function installBootLandingRedirect(deps: BootLandingDeps): void {
	if (isBootLandingApplied()) return;

	const pathname = deps.getPathname();
	if (!shouldApplyBootLanding(pathname, false)) {
		markBootLandingApplied();
		return;
	}

	const nowMs = deps.nowMs ?? (() => Date.now());
	const target = resolveBootLandingRoute(deps.readBootStamp(), nowMs());
	markBootLandingApplied();
	if (target !== pathname) {
		void deps.goto(target, { replaceState: true });
	}
}
