<script lang="ts">
	/**
	 * OJ's brand in the agent sidebar header (issue #13711): the glass, the OJ heading, the
	 * free-preview badge, and the panel's juice fill. The fill is absolutely placed against
	 * the sidebar's aside (positioned there) and sits under its content. Props only:
	 * AgentSidebar.svelte decides the look through ojMotionPolicy and the loading variant.
	 */
	import type { AssistantStatus } from '$lib/assistant/assistant-api';
	import OjGlass from './OjGlass.svelte';
	import type { OjAgentLook } from './oj-agent-header';
	import { ojFillLevel, ojFillShowsBubbles, ojFillShowsLayer, ojFillTransitionMs } from './oj-fill';

	let { look, mode = null }: { look: OjAgentLook; mode?: AssistantStatus['mode'] } = $props();

	const fillLevel = $derived(
		ojFillLevel({ phase: look.fillPhase, streamedChars: look.streamedChars })
	);
	const fillMs = $derived(ojFillTransitionMs(look.fillPhase));
	const showFill = $derived(ojFillShowsLayer(look.fillMode));
	const showBubbles = $derived(ojFillShowsBubbles(look.fillMode, look.fillPhase));
</script>

{#if showFill}
	<div
		class="oj-agent-fill"
		aria-hidden="true"
		data-oj-fill-mode={look.fillMode}
		data-oj-fill-phase={look.fillPhase}
		data-oj-fill-level={fillLevel.toFixed(3)}
	>
		<div
			class="oj-agent-juice"
			class:animated={look.fillMode === 'animated'}
			style:--oj-fill-level={fillLevel}
			style:--oj-fill-ms="{fillMs}ms"
		></div>
		{#if showBubbles}
			<span class="oj-bubble"></span><span class="oj-bubble"></span><span class="oj-bubble"></span>
		{/if}
	</div>
{/if}
<OjGlass state={look.glass} bubbles={look.glassBubbles} policy={look.motion} size={24} />
<h2>OJ</h2>
{#if mode === 'gateway'}
	<span class="plan" title="OJ is free during the preview; a spend cap per install applies.">
		Free preview
	</span>
{/if}

<style>
	h2 {
		margin: 0;
		font-size: 1em;
		font-weight: 600;
	}
	.plan {
		color: var(--sb-muted);
		font-size: 0.82em;
		border: 1px solid var(--sb-line);
		border-radius: 99px;
		padding: 0 7px;
		cursor: help;
	}
	.oj-agent-fill {
		position: absolute;
		inset: 0;
		z-index: -1;
		overflow: hidden;
		pointer-events: none;
	}
	.oj-agent-juice {
		position: absolute;
		inset: 0;
		background: color-mix(in srgb, var(--accent) 14%, transparent);
		transform: translateY(calc((1 - var(--oj-fill-level, 0)) * 100%));
		will-change: transform;
	}
	.oj-agent-juice.animated {
		transition: transform var(--oj-fill-ms, 0ms) linear;
	}
	.oj-bubble {
		position: absolute;
		bottom: 0.5rem;
		width: 0.45rem;
		height: 0.45rem;
		border-radius: 50%;
		background: color-mix(in srgb, var(--accent) 50%, transparent);
		animation: oj-agent-bubble-rise 2.4s linear infinite;
	}
	.oj-bubble:nth-of-type(1) {
		left: 22%;
	}
	.oj-bubble:nth-of-type(2) {
		left: 48%;
		animation-delay: 0.8s;
	}
	.oj-bubble:nth-of-type(3) {
		left: 74%;
		animation-delay: 1.6s;
	}
	@keyframes oj-agent-bubble-rise {
		from {
			transform: translateY(0) scale(0.6);
			opacity: 1;
		}
		to {
			transform: translateY(-14rem) scale(1);
			opacity: 0;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.oj-agent-juice.animated {
			transition: none;
		}
		.oj-bubble {
			display: none;
		}
	}
	:global(html[data-reduce-motion]) .oj-agent-juice.animated {
		transition: none;
	}
	:global(html[data-reduce-motion]) .oj-bubble {
		display: none;
	}
</style>
