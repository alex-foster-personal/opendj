<script lang="ts">
	import { onMount } from 'svelte';
	import { prefersReducedMotion, watchReducedMotionMedia } from '$lib/motion/reduced-motion';
	import {
		createGestureScheduler,
		ojGlassClasses,
		type OjGesture,
		type OjGlassState
	} from './oj-glass-gestures';
	import { OJ_MOTION_FULL } from './oj-glass-motion';
	import { ojMotionPolicy, type OjMotion } from './oj-motion-policy';
	import OjGlassGraphic from './OjGlassGraphic.svelte';

	let {
		state: glassState,
		bubbles,
		size,
		policy,
		clipId
	}: {
		state: OjGlassState;
		bubbles: boolean;
		size: number;
		policy: OjMotion | undefined;
		clipId: string;
	} = $props();

	let gesture = $state<OjGesture | null>(null);
	let envPolicy = $state<OjMotion>(OJ_MOTION_FULL);

	const effective = $derived(policy ?? envPolicy);
	const displayGesture = $derived(glassState === 'idle' && effective.idleGestures ? gesture : null);
	const classes = $derived(ojGlassClasses(glassState, displayGesture, effective).join(' '));

	onMount(() => {
		const compute = () => {
			envPolicy = ojMotionPolicy({
				// A hidden document is treated as reduced motion: nothing should animate offscreen.
				reducedMotion: prefersReducedMotion() || document.visibilityState === 'hidden',
				anyDeckPlaying: false,
				performanceVisible: false,
				streaming: false
			});
		};
		compute();
		const stopWatchingMotion = watchReducedMotionMedia(() => compute());
		const onVis = () => compute();
		document.addEventListener('visibilitychange', onVis);

		const scheduler = createGestureScheduler({
			getState: () => glassState,
			getPolicy: () => effective,
			onGesture: (g) => {
				gesture = g;
			}
		});
		scheduler.start();
		return () => {
			stopWatchingMotion();
			document.removeEventListener('visibilitychange', onVis);
			scheduler.stop();
		};
	});
</script>

<OjGlassGraphic {classes} state={glassState} {bubbles} {effective} {size} {clipId} />
