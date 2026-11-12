<script module lang="ts">
	let clipSeq = 0;
</script>

<script lang="ts">
	import { ojGlassClasses, type OjGlassState } from './oj-glass-gestures';
	import { OJ_MOTION_FULL } from './oj-glass-motion';
	import type { OjMotion } from './oj-motion-policy';
	import OjGlassClient from './OjGlassClient.svelte';
	import OjGlassGraphic from './OjGlassGraphic.svelte';

	let {
		state = 'idle',
		bubbles = false,
		size = 28,
		policy = undefined
	}: {
		state?: OjGlassState;
		bubbles?: boolean;
		size?: number;
		/** Override; when absent the client half derives one from prefers-reduced-motion and visibility. */
		policy?: OjMotion;
	} = $props();

	const clipId = `oj-glass-clip-${++clipSeq}`;

	const effective = $derived(policy ?? OJ_MOTION_FULL);
	const serverClasses = $derived(ojGlassClasses(state, null, effective).join(' '));
</script>

{#if typeof window !== 'undefined'}
	<OjGlassClient {state} {bubbles} {size} {policy} {clipId} />
{:else}
	<OjGlassGraphic classes={serverClasses} {state} {bubbles} {effective} {size} {clipId} />
{/if}
