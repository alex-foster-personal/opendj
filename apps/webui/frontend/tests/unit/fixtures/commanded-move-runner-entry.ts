/** The commanded-move runner with the knob registry and animation registry it
 * writes through, from ONE bundle, so a test's fake dials are the dials the
 * runner writes (see agent-orders-entry.ts for the same need). */
export * as runner from '$lib/rb/commanded-move-runner.svelte';
export * as knobs from '$lib/rb/knob-control.svelte';
export { dialAnimations } from '$lib/rb/dial-animations.svelte';
export * as motion from '$lib/motion/reduced-motion';
export * as presentation from '$lib/rb/commanded-move-presentation';
