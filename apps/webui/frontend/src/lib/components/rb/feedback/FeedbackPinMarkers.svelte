<script lang="ts">
	/**
	 * The comment-pin marker layer (#858): every drawn pin on this page, as a
	 * fixed-position glyph whose COLOR is its lifecycle status and whose click
	 * opens the pin body.
	 *
	 * Split out of FeedbackWidget.svelte so the marker's markup and the status
	 * palette that gives it meaning live together in one file. The widget owns
	 * which pins are drawn (archived pins are filtered out, never merely
	 * hidden) and what happens on open; this component owns how a pin looks.
	 */
	import { isPinUnread, pinStyle, type PinSeen } from '$lib/rb/feedback';
	import { pinVisualState } from '$lib/rb/feedback-pin-partial';
	import { API_BASE } from '$lib/api';
	import type { FeedbackPin } from '$lib/rb/feedback-store.svelte';

	let {
		pins,
		seen,
		onopen
	}: {
		pins: FeedbackPin[];
		seen: PinSeen;
		onopen: (pin: FeedbackPin) => void;
	} = $props();
</script>

{#each pins as pin (pin.id)}
	{@const state = pinVisualState(pin)}
	<button
		type="button"
		class="fb-pin fb-{state}"
		class:fb-agent={pin.author === 'agent'}
		class:fb-unread={isPinUnread(pin, seen)}
		style={pinStyle(pin)}
		title={`${pin.text} - ${pin.created_at}${pin.anchor ? ` (near ${pin.anchor})` : ''}`}
		aria-label={`Comment pin (${state}) - open`}
		onclick={() => onopen(pin)}
	>
		{#if pin.author === 'agent'}
			<svg class="fb-mark fb-robot" width="12" height="11" viewBox="0 0 12 11" aria-hidden="true">
				<rect x="2" y="3" width="8" height="6" rx="1" />
				<path d="M6 1v2M4 5h.01M8 5h.01M4 7h4" />
			</svg>
		{:else if state === 'partial'}
			<!-- half-fixed (pin 58a16ac781db): two copies of the same glyph,
			     each clipped to one half, so the split reads as two SOLID
			     colours rather than an opacity or border tint. Left = still
			     open (orange), right = the fixed half (green). -->
			<svg class="fb-mark fb-mark-half fb-mark-half-open" width="12" height="11" viewBox="0 0 12 11" aria-hidden="true">
				<path d="M1.5 1.5 h9 v6 h-4.5 l-2.5 2.4 v-2.4 h-2 z" />
			</svg>
			<svg class="fb-mark fb-mark-half fb-mark-half-fixed" width="12" height="11" viewBox="0 0 12 11" aria-hidden="true">
				<path d="M1.5 1.5 h9 v6 h-4.5 l-2.5 2.4 v-2.4 h-2 z" />
			</svg>
		{:else}
			<svg class="fb-mark" width="12" height="11" viewBox="0 0 12 11" aria-hidden="true">
				<path d="M1.5 1.5 h9 v6 h-4.5 l-2.5 2.4 v-2.4 h-2 z" />
			</svg>
		{/if}
		{#if pin.attachment}
			<img
				class="fb-pin-thumb"
				src={`${API_BASE}${pin.attachment.url}`}
				alt=""
				width="28"
				height="20"
			/>
		{/if}
	</button>
{/each}

<style>
	/* #858 pin lifecycle. open = amber (unchanged), issued = amber + link,
	   blocked = red only when the maintainer must act (auth, destructive decision, or
	   genuine product fork),
	   glyph, fixed = green OUTLINE, merged = SOLID green, archived = not
	   drawn at all (filtered out upstream, never merely hidden). partial
	   (pin 58a16ac781db, follow-on to #907) is a DERIVED state - never
	   persisted, see pinVisualState in feedback.ts - and renders half
	   orange/half green, never a single blended colour. */
	.fb-pin {
		position: fixed;
		z-index: 80;
		transform: translate(-50%, -50%);
		color: var(--rb-orange);
		cursor: pointer;
		background: none;
		border: none;
		padding: 0;
		line-height: 0;
	}
	.fb-mark path {
		fill: currentColor;
	}
	.fb-fixed,
	.fb-merged {
		color: var(--rb-green);
	}
	.fb-blocked {
		color: var(--rb-red);
	}
	/* Agent identity is purple while open/issued. Resolved lifecycle states
	   remain green like operator pins, while the robot shape keeps its source
	   clear. */
	.fb-agent { color: #a855f7; }
	.fb-agent.fb-fixed,
	.fb-agent.fb-merged { color: var(--rb-green); }
	.fb-robot { fill: currentColor; stroke: currentColor; stroke-width: 1; stroke-linecap: round; }
	.fb-partial {
		position: fixed; /* .fb-pin already sets this; kept explicit so the
		                     absolutely-positioned halves below have the
		                     right containing block even if that rule moves */
		width: 12px;
		height: 11px;
	}
	.fb-mark-half {
		position: absolute;
		top: 0;
		left: 0;
	}
	.fb-mark-half-open {
		color: var(--rb-orange);
		clip-path: inset(0 50% 0 0);
	}
	.fb-mark-half-fixed {
		color: var(--rb-green);
		clip-path: inset(0 0 0 50%);
	}
	.fb-fixed .fb-mark path {
		fill: none;
		stroke: currentColor;
		stroke-width: 1.3;
		stroke-linejoin: round;
	}
	.fb-merged .fb-mark path {
		fill: currentColor;
	}
	/* issued: amber still, plus the link mark that says it has been filed */
	.fb-issued::before {
		content: '';
		position: absolute;
		left: 7px;
		bottom: 0;
		width: 6px;
		height: 3px;
		border: 1px solid currentColor;
		border-radius: 2px;
	}
	/* the unread dot: an agent has written to this pin since the maintainer read it */
	.fb-unread::after {
		content: '';
		position: absolute;
		top: -2px;
		right: -3px;
		width: 5px;
		height: 5px;
		border-radius: 50%;
		background: var(--rb-accent);
	}
	.fb-pin-thumb {
		display: block;
		width: 28px;
		height: 20px;
		object-fit: cover;
		border: 1px solid currentColor;
		border-radius: 1px;
		margin-top: 2px;
		pointer-events: none;
	}
</style>
