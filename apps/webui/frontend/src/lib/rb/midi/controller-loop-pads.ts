/** Shared loop-pad lengths for actions and feedback selectors.
 * Supersedes the private banks in controller-pad-runtime.svelte.ts.
 * Source: djay Pro 5.6.8 Mixtour Pro factory mapping v2, documented with its
 * checksum in docs/controller/acceptance/mixtour-pro-2026-09-22.md.
 * Both normal and SHIFT banks use these lengths. Colors stay in device maps. */
export const CONTROLLER_LOOP_BEATS = {
	auto_loop: [0.25, 0.5, 1, 2, 4, 8, 16, 32],
	bounce_loop: [0.03125, 0.0625, 0.125, 0.25, 0.5, 1, 2, 4]
} as const;
