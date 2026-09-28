/**
 * Documented z-index stack for root overlays and the boot preflight gate.
 *
 * Issue #2709 fixed preflight-vs-setup; #2722 generalises the yield rule so
 * ANY modal overlay the user opens is never permanently buried under the boot
 * gate. Z-index values are pinned here so a unit test can catch drift.
 */
export const OVERLAY_Z_INDEX = {
	brandLaunch: 1200,
	/** Shell quit confirm (QuitConfirmOverlay.svelte). */
	quitConfirm: 10000,
	/** Always-on comment dock + armed placement capture above boot/quit gates (#3981). */
	feedbackDock: 10050,
	/** Draft pin bubble and saved pin card above root modals while dock placement is armed (#3981). */
	feedbackPinBubble: 10055,
	preflightBoot: 1000,
	/** Pin anchors and clusters; above root modals, below boot gate. */
	feedbackPinPlacement: 450,
	hotkeys: 410,
	settings: 400,
	signIn: 390,
	account: 385,
	setupBackdrop: 380,
	preflightStrip: 360,
	stage: 350,
	setupChip: 370
} as const;

export type OverlayStackId = keyof typeof OVERLAY_Z_INDEX;

/** Overlays that must lower the blocking boot gate while open (defense in depth). */
export const BOOT_GATE_YIELD_OVERLAYS: OverlayStackId[] = [
	'setupBackdrop',
	'settings',
	'account',
	'signIn'
];

export interface OverlayOpenState {
	setup: boolean;
	settings: boolean;
	account: boolean;
	signIn: boolean;
}

/** True when any overlay that must sit above the boot gate is open. */
export function bootGateYielded(overlays: OverlayOpenState): boolean {
	return overlays.setup || overlays.settings || overlays.account || overlays.signIn;
}

/**
 * Pairs where the "under" overlay would be painted below "over" if both stayed
 * open. The boot gate must yield (see shouldBlockOnPreflight) rather than rely
 * on z-index alone for these pairs.
 */
export const HIDDEN_BEHIND_PAIRS: { under: OverlayStackId; over: OverlayStackId }[] = [
	{ under: 'settings', over: 'preflightBoot' },
	{ under: 'account', over: 'preflightBoot' },
	{ under: 'signIn', over: 'preflightBoot' },
	{ under: 'setupBackdrop', over: 'preflightBoot' },
	{ under: 'setupBackdrop', over: 'settings' },
	{ under: 'stage', over: 'settings' }
];

/** Returns overlay ids sorted by z-index descending (highest first). */
export function overlaysByZIndex(openIds: OverlayStackId[]): OverlayStackId[] {
	return [...openIds].sort((a, b) => OVERLAY_Z_INDEX[b] - OVERLAY_Z_INDEX[a]);
}

/**
 * When multiple overlays are open, the highest z-index must be strictly greater
 * than every other open overlay, OR the boot gate must have yielded.
 */
export function highestOpenOverlayWins(
	openIds: OverlayStackId[],
	bootGateBlocking: boolean
): boolean {
	if (openIds.length <= 1) return true;
	// preflightBoot's own z-index (1000) always outranks every other overlay,
	// so its mere presence alongside another open overlay is only ever
	// correct while the boot gate is genuinely blocking (blockOnPreflight
	// true). If the caller reports bootGateBlocking=false -- i.e. the gate
	// has logically yielded -- while 'preflightBoot' is STILL in the open
	// list, that is exactly the contradiction #2722 P0-2 found: the
	// component never actually unmounted, so its z-index still buries
	// whatever the app thinks is now on top. Flag it regardless of the
	// numeric sort below.
	if (openIds.includes('preflightBoot')) {
		const others = openIds.filter((id) => id !== 'preflightBoot');
		if (others.length > 0 && !bootGateBlocking) return false;
	}
	const sorted = overlaysByZIndex(openIds);
	const highest = OVERLAY_Z_INDEX[sorted[0]];
	for (let i = 1; i < sorted.length; i += 1) {
		if (OVERLAY_Z_INDEX[sorted[i]] >= highest) return false;
	}
	if (bootGateBlocking && openIds.includes('preflightBoot')) {
		const others = openIds.filter((id) => id !== 'preflightBoot');
		if (others.length > 0) return false;
	}
	return true;
}
