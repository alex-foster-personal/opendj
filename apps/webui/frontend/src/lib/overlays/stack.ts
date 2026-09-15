/**
 * Overlay z-index registry (issue #2722, follow-up to #2709).
 *
 * One source of truth so the boot preflight gate cannot bury interactive
 * surfaces (setup wizard, settings) again. Preflight boot sits BELOW setup
 * and settings so when both are logically open the interactive layer wins.
 */
export const OVERLAY_Z = {
	stage: 350,
	preflightBoot: 360,
	preflightStrip: 360,
	setupBackdrop: 370,
	setupPanel: 380,
	account: 385,
	signIn: 390,
	settings: 400,
	hotkeys: 410,
	brandLaunch: 1200
} as const;
